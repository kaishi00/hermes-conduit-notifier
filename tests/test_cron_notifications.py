import importlib.util
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


store_module = _load("conduit_cron_store_test", ROOT / "cron_store.py")
api = _load("conduit_plugin_api_cron", ROOT / "dashboard" / "plugin_api.py")
CronStore = store_module.CronStore


def test_jobs_notify_until_muted_and_again_once_turned_back_on(tmp_path):
    store = CronStore(tmp_path)
    assert store.muted() == [] and not store.is_muted("abc")
    assert store.set_notify("abc", False) == ["abc"]
    assert store.set_notify("job-2", False) == ["abc", "job-2"]
    assert CronStore(tmp_path).is_muted("abc")
    assert store.set_notify("abc", True) == ["job-2"]
    assert (tmp_path / store_module.FILE_NAME).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("job_id, notify", [("", False), ("../x", False), (None, False), ("abc", "no")])
def test_bad_requests_are_refused(tmp_path, job_id, notify):
    with pytest.raises(ValueError):
        CronStore(tmp_path).set_notify(job_id, notify)


def test_the_list_keeps_the_newest_choices(tmp_path, monkeypatch):
    monkeypatch.setattr(store_module, "MAX_MUTED", 2)
    clock = iter(range(10))
    store = CronStore(tmp_path, clock=lambda: next(clock))
    for job in ("a", "b", "c"):
        store.set_notify(job, False)
    assert store.muted() == ["b", "c"]


def test_an_unreadable_file_reads_as_nothing_muted(tmp_path):
    (tmp_path / store_module.FILE_NAME).write_text("{not json")
    assert CronStore(tmp_path).muted() == []


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "_cron_home", lambda: tmp_path)
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


def test_routes_read_and_change_a_jobs_notifications(client):
    assert client.get(f"{BASE}/cron/notifications").json() == {"ok": True, "muted": []}
    response = client.put(f"{BASE}/cron/notifications", json={"job_id": "a1b2c3d4e5f6", "notify": False})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"ok": True, "muted": ["a1b2c3d4e5f6"]}
    assert client.get(f"{BASE}/cron/notifications").json()["muted"] == ["a1b2c3d4e5f6"]
    assert client.put(f"{BASE}/cron/notifications", json={"job_id": "a1b2c3d4e5f6", "notify": True}).json() \
        == {"ok": True, "muted": []}


def test_a_bad_body_is_a_400(client):
    assert client.put(f"{BASE}/cron/notifications", json={"job_id": "x", "notify": 1}).status_code == 400
    assert client.put(f"{BASE}/cron/notifications", json=["x"]).status_code == 400


def test_an_unreadable_file_is_not_overwritten(tmp_path, monkeypatch):
    store = CronStore(tmp_path)
    store.set_notify("a", False)
    store.set_notify("b", False)
    saved = store.path.read_text(encoding="utf-8")
    real = type(store.path).read_text

    def unreadable(path, *args, **kwargs):
        if path == store.path:
            raise PermissionError("busy")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(type(store.path), "read_text", unreadable)
    with pytest.raises(OSError):
        store.set_notify("c", False)
    monkeypatch.undo()
    assert store.path.read_text(encoding="utf-8") == saved
