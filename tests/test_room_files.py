import base64
import importlib.util
import os
import pathlib
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

_spec = importlib.util.spec_from_file_location("conduit_plugin_api_room_files", ROOT / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(api)


def _body(data: bytes, name: str = "photo.jpg"):
    return {"name": name, "data_base64": base64.b64encode(data).decode()}


def test_a_file_is_stored_under_its_own_folder_and_named_by_reference(tmp_path):
    stored = api.store_room_file(str(tmp_path), _body(b"hello", "notes.txt"), now=1_760_000_000)
    path = pathlib.Path(stored["path"])
    assert path.read_bytes() == b"hello"
    assert path.name == "notes.txt"
    assert path.parent.parent == tmp_path
    assert stored["size"] == 5
    assert stored["ref_text"] == f"@file:{path}"


def test_two_uploads_with_one_name_never_overwrite_each_other(tmp_path):
    first = api.store_room_file(str(tmp_path), _body(b"one"))
    second = api.store_room_file(str(tmp_path), _body(b"two"))
    assert first["path"] != second["path"]
    assert pathlib.Path(first["path"]).read_bytes() == b"one"


def test_names_cannot_leave_the_folder(tmp_path):
    stored = api.store_room_file(str(tmp_path), _body(b"x", "../../etc/passwd"))
    path = pathlib.Path(stored["path"])
    assert path.parent.parent == tmp_path
    assert "/" not in path.name and path.name.endswith("passwd")
    assert api._room_file_name("") == "file"
    assert api._room_file_name(" .. ") == "file"
    long = api._room_file_name("a" * 300 + ".pdf")
    assert len(long.encode()) == api.ROOM_FILE_NAME_MAX_BYTES and long.endswith(".pdf")
    wide = api._room_file_name("漢" * 120 + ".jpg")
    assert len(wide.encode()) <= api.ROOM_FILE_NAME_MAX_BYTES and wide.endswith(".jpg")
    assert api._room_file_name("a`b'c.txt") == "a_b_c.txt"


def test_a_cjk_name_is_stored(tmp_path):
    stored = api.store_room_file(str(tmp_path), _body(b"x", "写真" * 80 + ".jpg"))
    assert pathlib.Path(stored["path"]).read_bytes() == b"x"


def test_a_path_with_spaces_is_quoted_in_the_reference(tmp_path):
    stored = api.store_room_file(str(tmp_path), _body(b"x", "my photo.jpg"))
    assert stored["ref_text"] == f"@file:`{stored['path']}`"


def test_old_uploads_are_swept_on_the_next_one(tmp_path):
    old = api.store_room_file(str(tmp_path), _body(b"old"))
    folder = pathlib.Path(old["path"]).parent
    stale = 1_000_000.0
    os.utime(folder, (stale, stale))
    api.store_room_file(str(tmp_path), _body(b"new"), now=stale + api.ROOM_FILE_RETENTION_S + 1)
    assert not folder.exists()


def test_bad_input_is_refused(tmp_path):
    import pytest
    from fastapi import HTTPException

    for body in ([], {}, {"data_base64": ""}, {"data_base64": "not base64!"}):
        with pytest.raises(HTTPException) as caught:
            api.store_room_file(str(tmp_path), body)
        assert caught.value.status_code == 400


def test_the_route_stores_under_the_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "_room_files_root", lambda: str(tmp_path / "conduit" / "room-files"))
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    response = TestClient(app).post(f"{BASE}/room-files", json=_body(b"hi", "a.txt"))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["ok"] is True
    assert pathlib.Path(body["path"]).read_bytes() == b"hi"
    assert body["ref_text"].startswith("@file:")


def test_a_refused_upload_gives_its_slot_back(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "_room_files_root", lambda: str(tmp_path))
    monkeypatch.setattr(api, "_room_file_limiter", api._MintLimiter(1, 600.0))
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    client = TestClient(app)
    assert client.post(f"{BASE}/room-files", json={"data_base64": "!"}).status_code == 400
    assert client.post(f"{BASE}/room-files", json=_body(b"ok")).status_code == 200
    assert client.post(f"{BASE}/room-files", json=_body(b"ok")).status_code == 429
