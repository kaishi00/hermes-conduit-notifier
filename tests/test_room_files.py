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
    assert api._room_file_name("invoice\u202egpj.exe") == "invoice_gpj.exe"


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
    api._room_file_last_sweep.clear()
    api.store_room_file(str(tmp_path), _body(b"new"), now=stale + api.ROOM_FILE_RETENTION_S + 1)
    assert not folder.exists()


def test_the_oldest_uploads_go_once_the_total_cap_is_passed(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "ROOM_FILE_TOTAL_BYTES", 10)
    first = api.store_room_file(str(tmp_path), _body(b"aaaa"), now=1_000.0)
    os.utime(pathlib.Path(first["path"]).parent, (1_000.0, 1_000.0))
    second = api.store_room_file(str(tmp_path), _body(b"bbbb"), now=2_000.0)
    os.utime(pathlib.Path(second["path"]).parent, (2_000.0, 2_000.0))
    api._room_file_last_sweep.clear()
    api.store_room_file(str(tmp_path), _body(b"cccc"), now=3_000.0)
    assert not pathlib.Path(first["path"]).exists()
    assert pathlib.Path(second["path"]).exists()


def test_a_full_disk_refuses_the_upload(tmp_path, monkeypatch):
    import pytest
    from fastapi import HTTPException

    monkeypatch.setattr(api.shutil, "disk_usage", lambda _: types.SimpleNamespace(free=10))
    with pytest.raises(HTTPException) as caught:
        api.store_room_file(str(tmp_path), _body(b"x"))
    assert caught.value.status_code == 507


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


def test_a_full_disk_during_the_write_is_a_507_and_leaves_no_folder(tmp_path, monkeypatch):
    import errno

    import pytest


    real_os_open = os.open

    def full_disk(path, flags, *args, **kwargs):
        if flags & os.O_CREAT and str(path).startswith(str(tmp_path)):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_os_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(api.os, "open", full_disk)
    with pytest.raises(api.HTTPException) as refused:
        api.store_room_file(str(tmp_path), _body(b"hello"), now=1_760_000_000)
    assert refused.value.status_code == 507
    assert list(tmp_path.iterdir()) == []


def test_line_separators_never_reach_a_name():
    assert api._room_file_name("a\u2028b\u2029c\x85d.txt") == "a_b_c_d.txt"


def test_a_stored_file_is_private_to_the_host_user(tmp_path):
    stored = api.store_room_file(str(tmp_path), _body(b"hello"), now=1_760_000_000)
    assert os.stat(stored["path"]).st_mode & 0o777 == 0o600


def test_a_name_that_is_not_text_falls_back_to_file():
    assert api._room_file_name(["a" * 10_000]) == "file"
    assert api._room_file_name({"x": 1}) == "file"


def test_a_lone_surrogate_in_a_name_is_dropped():
    assert api._room_file_name("\ud800photo.jpg") == "photo.jpg"
