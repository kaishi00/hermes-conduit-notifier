import base64
import importlib.util
import os
import pathlib
import sys
import types

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"

if "hermes_constants" not in sys.modules:
    _hermes_constants = types.ModuleType("hermes_constants")
    _hermes_constants.get_hermes_home = lambda: pathlib.Path("/nonexistent-hermes-home")
    sys.modules["hermes_constants"] = _hermes_constants

_spec = importlib.util.spec_from_file_location("conduit_plugin_api_room_images", ROOT / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(api)

JPEG = b"\xff\xd8\xff\xe0" + b"jpeg-bytes"
PNG = b"\x89PNG\r\n\x1a\n" + b"png-bytes"


def _body(data: bytes):
    return {"data_base64": base64.b64encode(data).decode()}


def _client(monkeypatch, tmp_path):
    monkeypatch.setattr(api.rooms, "_room_images_root", lambda: str(tmp_path / "conduit" / "room-images"))
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


def test_a_picture_round_trips_and_lists_its_revision(tmp_path):
    stored = api.rooms.store_room_image(str(tmp_path), "room_1", _body(JPEG), now=1_760_000_000)
    assert stored["mime"] == "image/jpeg"
    assert api.rooms.list_room_images(str(tmp_path)) == {"room_1": stored["rev"]}
    record = api.rooms.read_room_image(str(tmp_path), "room_1")
    assert base64.b64decode(record["data_base64"]) == JPEG
    assert record["rev"] == stored["rev"]
    files = sorted(os.listdir(tmp_path))
    assert [name.rsplit(".", 1)[1] for name in files] == ["lock", "img", "json"]
    for name in files:
        assert os.stat(tmp_path / name).st_mode & 0o777 == 0o600


def test_replacing_a_picture_changes_its_revision(tmp_path):
    first = api.rooms.store_room_image(str(tmp_path), "room_1", _body(JPEG), now=1_760_000_000)
    second = api.rooms.store_room_image(str(tmp_path), "room_1", _body(PNG), now=1_760_000_001)
    assert first["rev"] != second["rev"]
    assert api.rooms.read_room_image(str(tmp_path), "room_1")["mime"] == "image/png"
    assert len(api.rooms.list_room_images(str(tmp_path))) == 1


def test_only_jpeg_and_png_are_accepted(tmp_path):
    with pytest.raises(HTTPException) as caught:
        api.rooms.store_room_image(str(tmp_path), "room_1", _body(b"GIF89a..."))
    assert caught.value.status_code == 415


def test_an_oversized_picture_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(api.rooms, "ROOM_IMAGE_MAX_BYTES", 8)
    with pytest.raises(HTTPException) as caught:
        api.rooms.store_room_image(str(tmp_path), "room_1", _body(JPEG))
    assert caught.value.status_code == 413


def test_the_oldest_records_go_past_the_room_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(api.rooms, "ROOM_IMAGE_MAX_ROOMS", 2)
    for index in range(3):
        api.rooms.store_room_image(str(tmp_path), f"room_{index}", _body(JPEG), now=1_760_000_000 + index)
        path = api.rooms._room_image_stem(str(tmp_path), f"room_{index}") + ".json"
        os.utime(path, (1_760_000_000 + index, 1_760_000_000 + index))
    assert set(api.rooms.list_room_images(str(tmp_path))) == {"room_1", "room_2"}
    # The evicted room's bytes went with its record.
    assert len([name for name in os.listdir(tmp_path) if name.endswith(".img")]) == 2


def test_listing_reads_no_picture_bytes(tmp_path, monkeypatch):
    api.rooms.store_room_image(str(tmp_path), "room_1", _body(JPEG))
    real = api.rooms._read_no_follow
    seen = []
    monkeypatch.setattr(api.rooms, "_read_no_follow", lambda path, limit: seen.append(path) or real(path, limit))
    assert set(api.rooms.list_room_images(str(tmp_path))) == {"room_1"}
    assert seen and all(path.endswith(".json") for path in seen)


def test_a_symlinked_record_is_not_followed(tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text('{"room_id": "room_1", "rev": "1"}')
    root = tmp_path / "images"
    root.mkdir()
    os.symlink(outside, api.rooms._room_image_stem(str(root), "room_1") + ".json")
    assert api.rooms.read_room_image(str(root), "room_1") is None


def test_stale_temp_files_are_swept_on_a_write(tmp_path):
    stale = tmp_path / ".room-image.abc.tmp"
    stale.write_bytes(b"x")
    os.utime(stale, (1_000, 1_000))
    api.rooms.store_room_image(str(tmp_path), "room_1", _body(JPEG), now=1_760_000_000)
    assert not stale.exists()


def test_orphaned_picture_bytes_are_swept_on_a_write(tmp_path):
    orphan = tmp_path / ("0" * 64 + ".img")
    orphan.write_bytes(JPEG)
    os.utime(orphan, (1_000, 1_000))
    api.rooms.store_room_image(str(tmp_path), "room_1", _body(JPEG), now=1_760_000_000)
    assert not orphan.exists()
    assert api.rooms.read_room_image(str(tmp_path), "room_1") is not None


def test_a_failed_record_write_drops_the_room(tmp_path, monkeypatch):
    api.rooms.store_room_image(str(tmp_path), "room_1", _body(JPEG), now=1_760_000_000)
    real = api.rooms._write_atomic

    def failing(root, path, data):
        if path.endswith(".json"):
            raise OSError(api.rooms.errno.ENOSPC, "full")
        return real(root, path, data)

    monkeypatch.setattr(api.rooms, "_write_atomic", failing)
    with pytest.raises(HTTPException) as caught:
        api.rooms.store_room_image(str(tmp_path), "room_1", _body(PNG), now=1_760_000_001)
    assert caught.value.status_code == 507
    assert api.rooms.read_room_image(str(tmp_path), "room_1") is None
    assert api.rooms.list_room_images(str(tmp_path)) == {}


def test_clearing_removes_only_that_room(tmp_path):
    api.rooms.store_room_image(str(tmp_path), "room_1", _body(JPEG))
    api.rooms.store_room_image(str(tmp_path), "room_2", _body(JPEG))
    assert api.rooms.clear_room_image(str(tmp_path), "room_1") is True
    assert api.rooms.clear_room_image(str(tmp_path), "room_1") is False
    assert set(api.rooms.list_room_images(str(tmp_path))) == {"room_2"}


def test_a_missing_folder_lists_nothing(tmp_path):
    assert api.rooms.list_room_images(str(tmp_path / "absent")) == {}


def test_routes_set_read_list_and_clear(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    put = client.put(f"{BASE}/room-images/room_9", json=_body(PNG))
    assert put.status_code == 200 and put.json()["mime"] == "image/png"
    rev = put.json()["rev"]
    assert client.get(f"{BASE}/room-images").json()["revisions"] == {"room_9": rev}
    got = client.get(f"{BASE}/room-images/room_9").json()
    assert base64.b64decode(got["data_base64"]) == PNG
    assert client.delete(f"{BASE}/room-images/room_9").json()["removed"] is True
    assert client.get(f"{BASE}/room-images/room_9").status_code == 404


def test_a_bad_room_id_is_refused(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    assert client.put(f"{BASE}/room-images/bad%20id", json=_body(PNG)).status_code == 400
    assert client.get(f"{BASE}/room-images/bad%20id").status_code == 400


def test_a_refused_picture_spends_no_rate_slot(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    monkeypatch.setattr(api.rooms, "_room_image_limiter", api.core._MintLimiter(1, 600.0))
    assert client.put(f"{BASE}/room-images/room_1", json=_body(b"nope")).status_code == 415
    assert client.put(f"{BASE}/room-images/room_1", json=_body(JPEG)).status_code == 200
    assert client.put(f"{BASE}/room-images/room_1", json=_body(JPEG)).status_code == 429
