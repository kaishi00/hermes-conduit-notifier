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


@pytest.fixture
def client(tmp_path, monkeypatch):
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
