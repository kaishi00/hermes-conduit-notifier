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
    stored = api.rooms.store_room_file(str(tmp_path), _body(b"hello", "notes.txt"), now=1_760_000_000)
    path = pathlib.Path(stored["path"])
    assert path.read_bytes() == b"hello"
    assert path.name == "notes.txt"
    assert path.parent.parent == tmp_path
    assert stored["size"] == 5
    assert stored["ref_text"] == f"@file:{path}"


def test_two_uploads_with_one_name_never_overwrite_each_other(tmp_path):
    first = api.rooms.store_room_file(str(tmp_path), _body(b"one"))
    second = api.rooms.store_room_file(str(tmp_path), _body(b"two"))
    assert first["path"] != second["path"]
    assert pathlib.Path(first["path"]).read_bytes() == b"one"


def test_names_cannot_leave_the_folder(tmp_path):
    stored = api.rooms.store_room_file(str(tmp_path), _body(b"x", "../../etc/passwd"))
    path = pathlib.Path(stored["path"])
    assert path.parent.parent == tmp_path
    assert "/" not in path.name and path.name.endswith("passwd")
    assert api.rooms._room_file_name("") == "file"
    assert api.rooms._room_file_name(" .. ") == "file"
    long = api.rooms._room_file_name("a" * 300 + ".pdf")
    assert len(long.encode()) == api.rooms.ROOM_FILE_NAME_MAX_BYTES and long.endswith(".pdf")
    wide = api.rooms._room_file_name("漢" * 120 + ".jpg")
    assert len(wide.encode()) <= api.rooms.ROOM_FILE_NAME_MAX_BYTES and wide.endswith(".jpg")
    assert api.rooms._room_file_name("a`b'c.txt") == "a_b_c.txt"
    assert api.rooms._room_file_name("invoice\u202egpj.exe") == "invoice_gpj.exe"


def test_a_cjk_name_is_stored(tmp_path):
    stored = api.rooms.store_room_file(str(tmp_path), _body(b"x", "写真" * 80 + ".jpg"))
    assert pathlib.Path(stored["path"]).read_bytes() == b"x"


def test_a_path_with_spaces_is_quoted_in_the_reference(tmp_path):
    stored = api.rooms.store_room_file(str(tmp_path), _body(b"x", "my photo.jpg"))
    assert stored["ref_text"] == f"@file:`{stored['path']}`"


def test_old_uploads_are_swept_on_the_next_one(tmp_path):
    old = api.rooms.store_room_file(str(tmp_path), _body(b"old"))
    folder = pathlib.Path(old["path"]).parent
    stale = 1_000_000.0
    os.utime(folder, (stale, stale))
    api.rooms._room_file_last_sweep.clear()
    api.rooms.store_room_file(str(tmp_path), _body(b"new"), now=stale + api.rooms.ROOM_FILE_RETENTION_S + 1)
    assert not folder.exists()


def test_the_oldest_uploads_go_once_the_total_cap_is_passed(tmp_path, monkeypatch):
    monkeypatch.setattr(api.rooms, "ROOM_FILE_TOTAL_BYTES", 10)
    first = api.rooms.store_room_file(str(tmp_path), _body(b"aaaa"), now=1_000.0)
    os.utime(pathlib.Path(first["path"]).parent, (1_000.0, 1_000.0))
    second = api.rooms.store_room_file(str(tmp_path), _body(b"bbbb"), now=2_000.0)
    os.utime(pathlib.Path(second["path"]).parent, (2_000.0, 2_000.0))
    api.rooms._room_file_last_sweep.clear()
    api.rooms.store_room_file(str(tmp_path), _body(b"cccc"), now=3_000.0)
    assert not pathlib.Path(first["path"]).exists()
    assert pathlib.Path(second["path"]).exists()


def test_a_full_disk_refuses_the_upload(tmp_path, monkeypatch):
    import pytest
    from fastapi import HTTPException

    monkeypatch.setattr(api.rooms.shutil, "disk_usage", lambda _: types.SimpleNamespace(free=10))
    with pytest.raises(HTTPException) as caught:
        api.rooms.store_room_file(str(tmp_path), _body(b"x"))
    assert caught.value.status_code == 507


def test_bad_input_is_refused(tmp_path):
    import pytest
    from fastapi import HTTPException

    for body in ([], {}, {"data_base64": ""}, {"data_base64": "not base64!"}):
        with pytest.raises(HTTPException) as caught:
            api.rooms.store_room_file(str(tmp_path), body)
        assert caught.value.status_code == 400


def test_the_route_stores_under_the_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setattr(api.rooms, "_room_files_root", lambda: str(tmp_path / "conduit" / "room-files"))
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
    monkeypatch.setattr(api.rooms, "_room_files_root", lambda: str(tmp_path))
    monkeypatch.setattr(api.rooms, "_room_file_limiter", api.core._MintLimiter(1, 600.0))
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

    monkeypatch.setattr(api.rooms.os, "open", full_disk)
    with pytest.raises(api.rooms.HTTPException) as refused:
        api.rooms.store_room_file(str(tmp_path), _body(b"hello"), now=1_760_000_000)
    assert refused.value.status_code == 507
    assert list(tmp_path.iterdir()) == []


def test_line_separators_never_reach_a_name():
    assert api.rooms._room_file_name("a\u2028b\u2029c\x85d.txt") == "a_b_c_d.txt"


def test_a_stored_file_is_private_to_the_host_user(tmp_path):
    stored = api.rooms.store_room_file(str(tmp_path), _body(b"hello"), now=1_760_000_000)
    assert os.stat(stored["path"]).st_mode & 0o777 == 0o600


def test_a_name_that_is_not_text_falls_back_to_file():
    assert api.rooms._room_file_name(["a" * 10_000]) == "file"
    assert api.rooms._room_file_name({"x": 1}) == "file"


def test_a_lone_surrogate_in_a_name_is_dropped():
    assert api.rooms._room_file_name("\ud800photo.jpg") == "photo.jpg"


_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def test_a_stored_picture_reads_back_as_a_data_url(tmp_path):
    stored = api.rooms.store_room_file(str(tmp_path), _body(_JPEG, "photo.jpg"))
    read = api.rooms.read_room_file_picture(str(tmp_path), stored["path"])
    assert read["dataUrl"] == "data:image/jpeg;base64," + base64.b64encode(_JPEG).decode()
    assert read["size"] == len(_JPEG)


def test_picture_types_come_from_the_bytes_not_the_name(tmp_path):
    import pytest

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    webp = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 16
    heic = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 16
    heif = b"\x00\x00\x00\x18ftypmif1" + b"\x00" * 16
    avif = b"\x00\x00\x00\x1cftypavif" + b"\x00" * 16
    avif_in_mif1 = b"\x00\x00\x00\x20ftypmif1\x00\x00\x00\x00mif1avifmiaf" + b"\x00" * 8
    for data, mime in ((png, "image/png"), (webp, "image/webp"), (heic, "image/heic"), (heif, "image/heic"),
                       (avif, "image/avif"), (avif_in_mif1, "image/avif"), (b"GIF89a....", "image/gif")):
        stored = api.rooms.store_room_file(str(tmp_path), _body(data, "picture.txt"))
        assert api.rooms.read_room_file_picture(str(tmp_path), stored["path"])["dataUrl"].startswith(f"data:{mime};base64,")
    # A name claiming a picture its bytes aren't is no picture type.
    text = api.rooms.store_room_file(str(tmp_path), _body(b"<svg onload=x>", "photo.jpg"))
    assert api.rooms.read_room_file_picture(str(tmp_path), text["path"])["dataUrl"].startswith(
        "data:application/octet-stream;base64,")


def test_any_other_file_reads_back_typed_by_its_name(tmp_path):
    for name, data, mime in (
        ("report.pdf", b"%PDF-1.7 ...", "application/pdf"),
        ("pasted_content_1.txt", "Hé, long paste".encode(), "text/plain"),
        # The host's own table can name audio differently; it's still audio.
        ("clip.m4a", b"\x00\x00\x00\x20ftypM4A ", __import__("mimetypes").guess_type("clip.m4a")[0]),
        ("notes", b"no extension", "application/octet-stream"),
    ):
        stored = api.rooms.store_room_file(str(tmp_path), _body(data, name))
        read = api.rooms.read_room_file_picture(str(tmp_path), stored["path"])
        assert read["dataUrl"] == f"data:{mime};base64," + base64.b64encode(data).decode(), name


def test_only_stored_room_files_are_read(tmp_path):
    import pytest

    root = tmp_path / "room-files"
    stored = api.rooms.store_room_file(str(root), _body(_JPEG))
    outside = tmp_path / "secret.jpg"
    outside.write_bytes(_JPEG)
    (root / "loose.jpg").write_bytes(_JPEG)
    folder = pathlib.Path(stored["path"]).parent
    (folder / "link.jpg").symlink_to(outside)
    cases = {
        str(outside): 404,
        str(root / "loose.jpg"): 404,
        str(folder / ".." / ".." / "secret.jpg"): 404,
        str(folder / "link.jpg"): 404,
        str(folder): 404,
        str(folder / "gone.jpg"): 404,
        "relative/photo.jpg": 400,
        "": 400,
        None: 400,
    }
    for path, status in cases.items():
        with pytest.raises(api.rooms.HTTPException) as refused:
            api.rooms.read_room_file_picture(str(root), path)
        assert refused.value.status_code == status, path


def test_a_picture_too_large_for_a_data_url_is_refused(tmp_path, monkeypatch):
    import pytest

    monkeypatch.setattr(api.rooms, "ROOM_FILE_PICTURE_MAX_BYTES", 32)
    stored = api.rooms.store_room_file(str(tmp_path), _body(_JPEG))
    with pytest.raises(api.rooms.HTTPException) as refused:
        api.rooms.read_room_file_picture(str(tmp_path), stored["path"])
    assert refused.value.status_code == 413


def test_the_route_reads_from_the_hermes_home(tmp_path, monkeypatch):
    root = tmp_path / "conduit" / "room-files"
    monkeypatch.setattr(api.rooms, "_room_files_root", lambda: str(root))
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    client = TestClient(app)
    path = client.post(f"{BASE}/room-files", json=_body(_JPEG)).json()["path"]
    response = client.get(f"{BASE}/room-files/data-url", params={"path": path})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["dataUrl"].startswith("data:image/jpeg;base64,")
    missing = client.get(f"{BASE}/room-files/data-url", params={"path": str(tmp_path / "x.jpg")})
    assert missing.status_code == 404
    assert missing.headers["cache-control"] == "no-store"


import pytest as _pytest


@_pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs here")
def test_a_fifo_in_an_upload_folder_is_turned_away_without_hanging(tmp_path):
    import pytest

    stored = api.rooms.store_room_file(str(tmp_path), _body(_JPEG))
    fifo = pathlib.Path(stored["path"]).parent / "pipe.jpg"
    os.mkfifo(fifo)
    with pytest.raises(api.rooms.HTTPException) as refused:
        api.rooms.read_room_file_picture(str(tmp_path), str(fifo))
    assert refused.value.status_code == 404


def test_picture_reads_are_rate_limited(tmp_path, monkeypatch):
    monkeypatch.setattr(api.rooms, "_room_files_root", lambda: str(tmp_path))
    monkeypatch.setattr(api.rooms, "_room_file_picture_limiter", api.core._MintLimiter(1, 60.0))
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    client = TestClient(app)
    path = api.rooms.store_room_file(str(tmp_path), _body(_JPEG))["path"]
    assert client.get(f"{BASE}/room-files/data-url", params={"path": path}).status_code == 200
    limited = client.get(f"{BASE}/room-files/data-url", params={"path": path})
    assert limited.status_code == 429
    assert limited.headers["cache-control"] == "no-store"
