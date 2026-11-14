"""Saved artifacts (artifact_pins.py, /artifacts/pins; hermes-conduit#2467)."""

import importlib.util
import os
import pathlib
import sys
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"

if "hermes_constants" not in sys.modules:
    _hermes_constants = types.ModuleType("hermes_constants")
    _hermes_constants.get_hermes_home = lambda: pathlib.Path(os.environ["HERMES_HOME"])
    sys.modules["hermes_constants"] = _hermes_constants

_spec = importlib.util.spec_from_file_location("conduit_push_artifact_pins_test", ROOT / "artifact_pins.py")
pins = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pins)
_api_spec = importlib.util.spec_from_file_location("conduit_plugin_api_artifact_pins", ROOT / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(_api_spec)
_api_spec.loader.exec_module(api)


class Clock:
    def __init__(self, now=1_000.0):
        self.now = now

    def __call__(self):
        return self.now


def _file_row(path, **extra):
    return {"kind": "document", "source": "delivered", "path": str(path), "name": pathlib.Path(path).name,
            "session_id": "s1", "session_title": "Wedding site", "message_id": 7, "timestamp": 1_700_000_000.0,
            **extra}


def _link_row(url="https://example.com/docs"):
    return {"kind": "link", "source": "link", "url": url, "name": "Docs", "session_id": "s1",
            "session_title": "Wedding site", "message_id": 8, "timestamp": 1_700_000_100.0}


def test_a_saved_file_keeps_a_copy_that_opens_after_the_original_goes(tmp_path):
    home = tmp_path / "home"
    original = tmp_path / "work" / "report.md"
    original.parent.mkdir()
    original.write_text("# Report")
    store = pins.PinStore(home, clock=Clock())

    saved = store.save({"item": _file_row(original), "note": "that report"})["pin"]
    assert saved["exists"] is True
    assert saved["note"] == "that report"
    kept = pathlib.Path(saved["kept_path"])
    assert kept.read_text() == "# Report"
    assert kept.is_relative_to(home / pins.COPY_DIR)
    assert kept.name == "report.md"

    original.unlink()
    row = store.view()["pins"][0]
    assert row["exists"] is False
    assert row["kept_path"] == str(kept)
    assert row["session_title"] == "Wedding site"


def test_saving_again_changes_only_the_note(tmp_path):
    original = tmp_path / "a.pdf"
    original.write_bytes(b"one")
    clock = Clock()
    store = pins.PinStore(tmp_path / "home", clock=clock)
    first = store.save({"item": _file_row(original)})["pin"]
    original.write_bytes(b"two")
    clock.now = 2_000.0
    second = store.save({"item": _file_row(original), "note": "  the   final one "})["pin"]
    assert second["note"] == "the final one"
    assert second["pinned_at"] == first["pinned_at"]
    assert pathlib.Path(second["kept_path"]).read_bytes() == b"one"
    assert len(store.view()["pins"]) == 1


def test_a_link_is_saved_without_a_copy(tmp_path):
    store = pins.PinStore(tmp_path, clock=Clock())
    row = store.save({"item": _link_row()})["pin"]
    assert row["url"] == "https://example.com/docs"
    assert "kept_path" not in row and "exists" not in row
    with pytest.raises(pins.PinError):
        store.save({"item": _link_row("https://user:pw@example.com/")})


def test_the_one_saved_last_is_listed_first(tmp_path):
    clock = Clock()
    store = pins.PinStore(tmp_path, clock=clock)
    store.save({"item": _link_row("https://example.com/1")})
    clock.now = 2_000.0
    store.save({"item": _link_row("https://example.com/2")})
    assert [pin["url"] for pin in store.view()["pins"]] == ["https://example.com/2", "https://example.com/1"]


def test_unsaving_removes_the_row_and_its_copy(tmp_path):
    original = tmp_path / "pic.png"
    original.write_bytes(b"png")
    store = pins.PinStore(tmp_path / "home", clock=Clock())
    kept = pathlib.Path(store.save({"item": _file_row(original)})["pin"]["kept_path"])
    assert store.remove(str(original)) == {"removed": True}
    assert not kept.exists() and not kept.parent.exists()
    assert store.view()["pins"] == []
    assert store.remove(str(original)) == {"removed": False}


def test_a_file_too_large_or_gone_is_saved_without_a_copy(tmp_path, monkeypatch):
    big = tmp_path / "big.mov"
    big.write_bytes(b"x" * 20)
    monkeypatch.setattr(pins, "MAX_COPY_BYTES", 10)
    store = pins.PinStore(tmp_path / "home", clock=Clock())
    row = store.save({"item": _file_row(big)})["pin"]
    assert row["not_kept"] == "too_large" and "kept_path" not in row
    gone = store.save({"item": _file_row(tmp_path / "gone.txt")})["pin"]
    assert gone["exists"] is False and gone["not_kept"] == "missing"


def test_copies_stop_at_the_total_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(pins, "MAX_TOTAL_COPY_BYTES", 15)
    store = pins.PinStore(tmp_path / "home", clock=Clock())
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_bytes(b"x" * 10)
    assert "kept_path" in store.save({"item": _file_row(tmp_path / "a.txt")})["pin"]
    assert store.save({"item": _file_row(tmp_path / "b.txt")})["pin"]["not_kept"] == "no_room"


def test_a_folder_or_relative_path_is_refused_or_not_copied(tmp_path):
    store = pins.PinStore(tmp_path / "home", clock=Clock())
    folder = tmp_path / "dir"
    folder.mkdir()
    assert store.save({"item": _file_row(folder)})["pin"]["not_kept"] == "not_a_file"
    with pytest.raises(pins.PinError):
        store.save({"item": _file_row("relative/file.txt")})
    with pytest.raises(pins.PinError):
        store.save({"item": _file_row(tmp_path / "x.txt"), "note": "n" * (pins.MAX_NOTE_CHARS + 1)})


def test_the_file_is_private_and_a_corrupt_one_reads_empty(tmp_path):
    store = pins.PinStore(tmp_path, clock=Clock())
    store.save({"item": _link_row()})
    assert store.path.stat().st_mode & 0o777 == 0o600
    store.path.write_text("{not json")
    assert store.view() == {"pins": []}


def test_a_removed_row_never_deletes_outside_its_copies(tmp_path):
    store = pins.PinStore(tmp_path / "home", clock=Clock())
    outside = tmp_path / "keep" / "me.txt"
    outside.parent.mkdir()
    outside.write_text("mine")
    store._remove_copy({"copy": str(outside)})
    assert outside.exists()


# --- Routes ---------------------------------------------------------------------


def _hermes_file_routes(monkeypatch, backend=None, locked_root=None):
    """Hermes' own file routing, as a local install has it."""
    files = types.ModuleType("hermes_cli.web_routers.files")
    files._fs_backend = lambda profile: backend
    files._fs_profile_home = lambda profile: None
    files._is_sensitive_path = lambda path: False
    monkeypatch.setitem(sys.modules, "hermes_cli.web_routers.files", files)
    server_files = types.ModuleType("hermes_cli.web_server_files")
    server_files._managed_files_policy = lambda *a, **k: types.SimpleNamespace(locked_root=locked_root)
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server_files", server_files)


@pytest.fixture
def client(tmp_path, monkeypatch):
    _hermes_file_routes(monkeypatch)
    monkeypatch.setattr(api.core, "_calls_home", lambda: tmp_path / "home")
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


def test_routes_save_list_note_and_unsave(client, tmp_path):
    original = tmp_path / "page.html"
    original.write_text("<p>hi</p>")
    saved = client.post(f"{BASE}/artifacts/pins", json={"item": _file_row(original, kind="page")})
    assert saved.status_code == 200, saved.text
    assert saved.headers["cache-control"] == "no-store"
    assert saved.json()["pin"]["kept_path"].endswith("page.html")

    client.post(f"{BASE}/artifacts/pins", json={"item": _file_row(original, kind="page"), "note": "widget"})
    listed = client.get(f"{BASE}/artifacts/pins").json()
    assert listed["ok"] is True
    assert [pin["note"] for pin in listed["pins"]] == ["widget"]

    removed = client.delete(f"{BASE}/artifacts/pins", params={"id": str(original)})
    assert removed.json() == {"ok": True, "removed": True}
    assert client.get(f"{BASE}/artifacts/pins").json()["pins"] == []


def test_route_refuses_a_bad_row(client):
    answer = client.post(f"{BASE}/artifacts/pins", json={"item": {"kind": "nope"}})
    assert answer.status_code == 400


def test_capabilities_list_artifact_pins():
    assert "artifact-pins" in api.capabilities.ROUTE_CAPABILITIES


def test_a_hosted_picture_is_saved_by_its_url(tmp_path):
    store = pins.PinStore(tmp_path, clock=Clock())
    row = {**_link_row("https://cdn.example.com/cat.png"), "kind": "image", "name": "cat.png"}
    saved = store.save({"item": row})["pin"]
    assert saved["kind"] == "image" and saved["url"] == "https://cdn.example.com/cat.png"
    assert "kept_path" not in saved and "exists" not in saved
    assert store.remove("https://cdn.example.com/cat.png") == {"removed": True}


def test_a_secret_file_is_saved_without_a_copy(tmp_path):
    secret = tmp_path / ".env"
    secret.write_text("TOKEN=1")
    link = tmp_path / "notes.txt"
    link.symlink_to(secret)
    store = pins.PinStore(tmp_path / "home", clock=Clock(),
                          sensitive=lambda path: path.endswith(".env"))
    assert store.save({"item": _file_row(secret)})["pin"]["not_kept"] == "sensitive"
    assert store.save({"item": _file_row(link)})["pin"]["not_kept"] == "sensitive", "checked where it leads too"
    assert not (tmp_path / "home" / pins.COPY_DIR).exists()


def test_a_save_that_lost_a_race_removes_only_its_own_copy(tmp_path):
    original = tmp_path / "a.txt"
    original.write_text("one")
    store = pins.PinStore(tmp_path / "home", clock=Clock())
    first = store._copy(str(original), str(original))[0]
    second = store._copy(str(original), str(original))[0]
    assert pathlib.Path(first).parent != pathlib.Path(second).parent
    store._remove_copy({"copy": second})
    assert pathlib.Path(first).read_text() == "one"


def test_copies_in_flight_count_against_the_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(pins, "MAX_TOTAL_COPY_BYTES", 15)
    store = pins.PinStore(tmp_path / "home", clock=Clock())
    (tmp_path / "b.txt").write_bytes(b"x" * 10)
    monkeypatch.setitem(pins._reserved, str(store.copies), 10)
    assert store.save({"item": _file_row(tmp_path / "b.txt")})["pin"]["not_kept"] == "no_room"


def test_a_long_name_fits_with_its_extension():
    name = pins._safe_name("é" * 200 + ".pdf")
    assert name.endswith(".pdf")
    assert len(name.encode("utf-8")) <= pins.MAX_COPY_NAME_BYTES
    assert pins._safe_name(".env") == "_.env"


def test_a_copy_a_crash_cut_short_is_swept(tmp_path):
    clock = Clock(now=100_000.0)
    store = pins.PinStore(tmp_path / "home", clock=clock)
    stale = store.copies / "abc-123"
    stale.mkdir(parents=True)
    temporary = stale / ".big.mov.tmp"
    temporary.write_bytes(b"x" * 10)
    os.utime(temporary, (1_000.0, 1_000.0))
    assert store._copies_size() == 0
    assert not stale.exists()


def test_a_finished_copy_named_tmp_is_never_swept(tmp_path):
    original = tmp_path / "render.tmp"
    original.write_bytes(b"x" * 10)
    clock = Clock()
    store = pins.PinStore(tmp_path / "home", clock=clock)
    kept = pathlib.Path(store.save({"item": _file_row(original)})["pin"]["kept_path"])
    os.utime(kept, (1.0, 1.0))
    clock.now = 1_000_000.0
    assert store._copies_size() == 10
    assert kept.exists()


def test_a_running_copys_temporary_counts_by_its_reservation_only(tmp_path):
    store = pins.PinStore(tmp_path / "home", clock=Clock(now=0.0))
    running = store.copies / "abc-123"
    running.mkdir(parents=True)
    (running / ".big.mov.tmp").write_bytes(b"x" * 10)
    assert store._copies_size() == 0
    assert running.exists()


def test_a_stray_temporary_at_the_root_never_sweeps_the_store(tmp_path):
    original = tmp_path / "a.txt"
    original.write_text("one")
    clock = Clock(now=100_000.0)
    store = pins.PinStore(tmp_path / "home", clock=clock)
    kept = pathlib.Path(store.save({"item": _file_row(original)})["pin"]["kept_path"])
    stray = store.copies / ".stray.tmp"
    stray.write_bytes(b"x")
    os.utime(stray, (1_000.0, 1_000.0))
    store._copies_size()
    assert kept.exists()


def test_a_hosted_install_copies_nothing_past_its_folder(client, tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    (root / "in.md").write_text("in")
    (tmp_path / "outside.md").write_text("out")
    (root / "link.md").symlink_to(tmp_path / "outside.md")
    _hermes_file_routes(monkeypatch, locked_root=root)

    def save(path):
        return client.post(f"{BASE}/artifacts/pins", json={"item": _file_row(path)}).json()["pin"]

    assert pathlib.Path(save(root / "in.md")["kept_path"]).read_text() == "in"
    assert save(tmp_path / "outside.md")["not_kept"] == "outside"
    assert save(root / "link.md")["not_kept"] == "outside", "a link inside to a file outside is outside"


def test_an_ssh_profiles_file_is_saved_without_a_copy(client, tmp_path, monkeypatch):
    (tmp_path / "same-name.md").write_text("this host's, not the profile's")
    _hermes_file_routes(monkeypatch, backend=object())
    pin = client.post(f"{BASE}/artifacts/pins", json={"item": _file_row(tmp_path / "same-name.md")}).json()["pin"]
    assert pin["not_kept"] == "remote" and "kept_path" not in pin


def test_without_hermes_routing_nothing_is_copied(client, tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text("x")
    monkeypatch.delitem(sys.modules, "hermes_cli.web_routers.files")
    pin = client.post(f"{BASE}/artifacts/pins", json={"item": _file_row(tmp_path / "a.md")}).json()["pin"]
    assert "kept_path" not in pin


def test_saves_past_both_copy_slots_are_turned_away_and_slots_come_back(client, tmp_path):
    original = tmp_path / "a.md"
    original.write_text("x")
    slots = api.workspace._artifact_pins_slots
    assert slots.acquire(blocking=False) and slots.acquire(blocking=False)
    try:
        assert client.post(f"{BASE}/artifacts/pins", json={"item": _file_row(original)}).status_code == 503
    finally:
        slots.release()
        slots.release()
    assert client.post(f"{BASE}/artifacts/pins", json={"item": _file_row(original)}).status_code == 200
    # Both free again: the save gave its slot back exactly once.
    assert slots.acquire(blocking=False) and slots.acquire(blocking=False)
    slots.release()
    slots.release()
    with pytest.raises(ValueError):
        slots.release()


def test_a_file_that_grows_while_it_copies_is_not_kept(tmp_path, monkeypatch):
    original = tmp_path / "log.txt"
    original.write_bytes(b"x" * 20)
    real_stat = os.stat

    def stale_stat(path, *args, **kwargs):
        info = real_stat(path, *args, **kwargs)
        if str(path) == str(original):
            values = list(info)
            values[6] = 10  # st_size, as it was before it grew
            return os.stat_result(values)
        return info

    monkeypatch.setattr(pins.os, "stat", stale_stat)
    store = pins.PinStore(tmp_path / "home", clock=Clock())
    assert store.save({"item": _file_row(original)})["pin"]["not_kept"] == "changing"
    assert list(store.copies.iterdir()) == []
    assert pins._reserved == {}


def test_a_row_that_fails_to_write_takes_its_copy_with_it(tmp_path, monkeypatch):
    original = tmp_path / "a.txt"
    original.write_text("one")
    store = pins.PinStore(tmp_path / "home", clock=Clock())

    def fail(encoded):
        raise OSError("disk full")

    monkeypatch.setattr(store, "_save", fail)
    with pytest.raises(OSError):
        store.save({"item": _file_row(original)})
    assert list(store.copies.iterdir()) == []


def test_a_path_that_is_not_text_is_refused_before_anything_is_reserved(tmp_path):
    store = pins.PinStore(tmp_path / "home", clock=Clock())
    with pytest.raises(pins.PinError):
        store.save({"item": _file_row(str(tmp_path) + "/bad\udcff.txt")})
    assert pins._reserved == {}
