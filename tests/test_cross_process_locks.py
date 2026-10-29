"""Writers in two processes (two dashboards, or a dashboard and the agent)
wait on one another through a lock file, not only on an in-process lock."""
import base64
import builtins
import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the helper process uses flock")

if "hermes_constants" not in sys.modules:
    _hermes_constants = types.ModuleType("hermes_constants")
    _hermes_constants.get_hermes_home = lambda: pathlib.Path(tempfile.gettempdir())
    sys.modules["hermes_constants"] = _hermes_constants
if "conduit_push" not in sys.modules:
    _pkg = types.ModuleType("conduit_push")
    _pkg.__path__ = [str(ROOT)]
    sys.modules["conduit_push"] = _pkg


def _load(name, path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


health = _load("conduit_push.health_store", ROOT / "health_store.py")
location = _load("conduit_push.location_store", ROOT / "location_store.py")
phone = _load("conduit_push.phone_store", ROOT / "phone_store.py")
_api_spec = importlib.util.spec_from_file_location("conduit_plugin_api_locks", ROOT / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(_api_spec)
_api_spec.loader.exec_module(api)

JPEG = b"\xff\xd8\xff\xe0" + b"jpeg-bytes"

_HOLDER = """
import fcntl, sys
with open(sys.argv[1], "a+b") as handle:
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    print("held", flush=True)
    sys.stdin.read()
"""


class OtherProcess:
    """Another process holding ``lock_path`` until released."""

    def __init__(self, lock_path):
        self.process = subprocess.Popen([sys.executable, "-c", _HOLDER, str(lock_path)],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            assert self.process.stdout.readline().strip() == "held"
        except BaseException:
            self.process.kill()
            raise

    def release(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            raise


def _waits_for(lock_path, action):
    """Runs ``action`` while another process holds ``lock_path``; it must not
    finish until the lock is released."""
    other = OtherProcess(lock_path)
    done = threading.Event()
    result = {}

    def run():
        try:
            result["value"] = action()
        except BaseException as exc:  # re-raised below
            result["error"] = exc
        done.set()

    thread = threading.Thread(target=run, daemon=True)
    try:
        thread.start()
        if done.wait(0.5) and "error" in result:
            raise result["error"]
        assert not done.is_set(), "ran while another process held the lock"
    finally:
        other.release()
    thread.join(10)
    assert done.is_set()
    if "error" in result:
        raise result["error"]
    return result["value"]


def _lock_of(store):
    return store.path.with_name(f".{store.path.name}.lock")


@pytest.mark.parametrize("make", [
    lambda home: health.HealthStore(home),
    lambda home: location.LocationStore(home),
    lambda home: phone.PhoneStore(home),
], ids=["health", "location", "phone"])
def test_a_wipe_waits_for_another_process_before_it_counts_and_unlinks(tmp_path, make):
    store = make(tmp_path)
    with store._write():
        pass
    assert store.path.exists()
    _waits_for(_lock_of(store), store.wipe)
    assert not store.path.exists()
    # The lock file stays, so a writer waiting on it never races the unlink.
    assert _lock_of(store).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("make", [
    lambda home: health.HealthStore(home),
    lambda home: location.LocationStore(home),
    lambda home: phone.PhoneStore(home),
], ids=["health", "location", "phone"])
def test_a_write_waits_for_another_process(tmp_path, make):
    store = make(tmp_path)

    def write():
        with store._write():
            return True

    assert _waits_for(_lock_of(store), write)


def test_a_room_picture_write_and_read_wait_for_another_process(tmp_path):
    root = str(tmp_path / "room-images")
    body = {"data_base64": base64.b64encode(JPEG).decode()}
    api.rooms.store_room_image(root, "room_1", body, now=1_760_000_000)
    lock = pathlib.Path(root) / api.rooms.ROOM_IMAGE_LOCK_FILE
    assert lock.stat().st_mode & 0o777 == 0o600
    stored = _waits_for(lock, lambda: api.rooms.store_room_image(root, "room_1", body, now=1_760_000_001))
    record = _waits_for(lock, lambda: api.rooms.read_room_image(root, "room_1"))
    assert record["rev"] == stored["rev"]
    assert _waits_for(lock, lambda: api.rooms.clear_room_image(root, "room_1"))


def test_reading_a_room_picture_with_no_folder_creates_nothing(tmp_path):
    root = tmp_path / "room-images"
    assert api.rooms.read_room_image(str(root), "room_1") is None
    assert api.rooms.clear_room_image(str(root), "room_1") is False
    assert not root.exists()


def test_an_unreadable_desktop_views_file_is_never_overwritten(tmp_path, monkeypatch):
    path = tmp_path / api.desktop_views.DESKTOP_VIEWS_FILE
    original = json.dumps({"version": 1, "views": {
        "a": {"opened_at": 100.0, "seen_through": 150.0, "client": "browser"}}})
    path.write_text(original)
    real_open = builtins.open

    def refusing_open(file, *args, **kwargs):
        if str(file) == str(path):
            raise PermissionError(13, "Permission denied", str(file))
        return real_open(file, *args, **kwargs)

    store = api.desktop_views._DesktopViewStore(flush_delay=3600.0)
    store.record(tmp_path, "b", "desktop", 210.0, opened_at=200.0)
    monkeypatch.setattr(builtins, "open", refusing_open)
    store.flush()  # logged, not raised
    monkeypatch.setattr(builtins, "open", real_open)
    assert path.read_text() == original


def test_a_room_picture_read_never_fails_on_a_lock_file_it_cannot_open(tmp_path):
    root = tmp_path / "room-images"
    (root / api.rooms.ROOM_IMAGE_LOCK_FILE).mkdir(parents=True)  # can't be opened for writing
    assert api.rooms.read_room_image(str(root), "room_1") is None
    assert api.rooms.clear_room_image(str(root), "room_1") is False
