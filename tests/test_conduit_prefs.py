import argparse
import importlib.util
import json
import pathlib
import sys
import tempfile
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"

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


store_module = _load("conduit_push.prefs_store", ROOT / "prefs_store.py")
_api_spec = importlib.util.spec_from_file_location("conduit_plugin_api_prefs", ROOT / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(_api_spec)
_api_spec.loader.exec_module(api)

PrefsStore = store_module.PrefsStore
PrefsError = store_module.PrefsError

PHONE = "phone-installation-1"
IPAD = "ipad-installation-2"


class Clock:
    def __init__(self, now=1_000.0):
        self.now = now

    def __call__(self):
        return self.now


def _store(tmp_path, clock=None):
    return PrefsStore(tmp_path, clock=clock or Clock())


def test_a_change_from_one_device_reaches_the_other(tmp_path):
    store = _store(tmp_path)
    store.sync({"installation_id": PHONE, "device": "iPhone", "changes": {"app.accent": "violet"}})
    seen = store.sync({"installation_id": IPAD, "changes": {}})
    assert seen["values"]["app.accent"]["value"] == "violet"
    assert seen["values"]["app.accent"]["by"] == PHONE
    assert seen["values"]["app.accent"]["by_name"] == "iPhone"


def test_the_later_change_wins_by_the_hosts_clock(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock)
    store.sync({"installation_id": PHONE, "changes": {"app.text_size": 2}})
    clock.now += 5
    after = store.sync({"installation_id": IPAD, "changes": {"app.text_size": 3}})
    assert after["values"]["app.text_size"] == {"value": 3, "at": 1_005.0, "by": IPAD, "by_name": "Conduit"}
    assert after["revision"] == 2


def test_null_clears_a_key(tmp_path):
    store = _store(tmp_path)
    store.sync({"installation_id": PHONE, "changes": {"app.accent": "violet", "app.haptics": False}})
    after = store.sync({"installation_id": PHONE, "changes": {"app.accent": None}})
    assert set(after["values"]) == {"app.haptics"}


def test_an_empty_sync_changes_nothing_on_disk(tmp_path):
    store = _store(tmp_path)
    store.sync({"installation_id": PHONE, "changes": {"app.accent": "violet"}})
    before = store.path.read_text()
    assert store.sync({"installation_id": IPAD})["revision"] == 1
    assert store.path.read_text() == before


@pytest.mark.parametrize("body", [
    [],
    {"changes": {}},
    {"installation_id": "x", "changes": {}},
    {"installation_id": PHONE, "changes": []},
    {"installation_id": PHONE, "changes": {"Bad Key": 1}},
    {"installation_id": PHONE, "changes": {"app.n": float("nan")}},
])
def test_malformed_syncs_are_refused(tmp_path, body):
    with pytest.raises(PrefsError):
        _store(tmp_path).sync(body)


def test_an_oversized_value_is_refused_and_nothing_is_kept(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(PrefsError) as error:
        store.sync({"installation_id": PHONE, "changes": {"app.ok": 1, "voice.default": "x" * (17 * 1024)}})
    assert error.value.code == "too_large"
    assert store.view()["values"] == {}


def test_too_many_keys_are_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(store_module, "MAX_KEYS", 2)
    store = _store(tmp_path)
    store.sync({"installation_id": PHONE, "changes": {"a.one": 1, "a.two": 2}})
    with pytest.raises(PrefsError) as error:
        store.sync({"installation_id": PHONE, "changes": {"a.three": 3}})
    assert error.value.code == "too_many"
    # Replacing a key that's there still fits.
    assert store.sync({"installation_id": PHONE, "changes": {"a.two": 4}})["values"]["a.two"]["value"] == 4


def test_export_and_import_move_the_set_to_another_host(tmp_path):
    first = _store(tmp_path / "first")
    first.sync({"installation_id": PHONE, "changes": {"app.accent": "violet", "voice.default": {"tone": "calm"}}})
    exported = json.loads(json.dumps(first.export()))
    assert exported["kind"] == "conduit-prefs"
    assert exported["values"] == {"app.accent": "violet", "voice.default": {"tone": "calm"}}

    second = _store(tmp_path / "second")
    second.sync({"installation_id": IPAD, "changes": {"app.accent": "green", "app.haptics": False}})
    merged = second.import_(exported)
    assert {key: entry["value"] for key, entry in merged["values"].items()} == {
        "app.accent": "violet", "voice.default": {"tone": "calm"}, "app.haptics": False}
    assert merged["values"]["app.accent"]["by"] == "import"

    replaced = second.import_(exported, replace=True)
    assert set(replaced["values"]) == {"app.accent", "voice.default"}


@pytest.mark.parametrize("document", [
    {},
    {"kind": "something-else", "version": 1, "values": {}},
    {"kind": "conduit-prefs", "version": 99, "values": {}},
    {"kind": "conduit-prefs", "version": 1},
])
def test_an_import_that_isnt_an_export_is_refused(tmp_path, document):
    with pytest.raises(PrefsError):
        _store(tmp_path).import_(document)


def test_a_corrupt_file_reads_as_empty(tmp_path):
    store = _store(tmp_path)
    store.path.write_text("{not json")
    assert store.view() == {"revision": 0, "values": {}}
    store.sync({"installation_id": PHONE, "changes": {"app.accent": "violet"}})
    assert store.view()["values"]["app.accent"]["value"] == "violet"


def test_the_file_is_private(tmp_path):
    store = _store(tmp_path)
    store.sync({"installation_id": PHONE, "changes": {"app.accent": "violet"}})
    assert store.path.stat().st_mode & 0o777 == 0o600


# --- Routes ---------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "_calls_home", lambda: tmp_path)
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


def test_routes_sync_view_export_and_import(client):
    put = client.put(f"{BASE}/conduit-prefs", json={"installation_id": PHONE, "changes": {"app.accent": "violet"}})
    assert put.status_code == 200
    assert put.headers["cache-control"] == "no-store"
    assert put.json()["values"]["app.accent"]["value"] == "violet"

    assert client.get(f"{BASE}/conduit-prefs").json()["revision"] == 1

    exported = client.get(f"{BASE}/conduit-prefs/export").json()["export"]
    assert exported["values"] == {"app.accent": "violet"}

    client.put(f"{BASE}/conduit-prefs", json={"installation_id": PHONE, "changes": {"app.haptics": False}})
    replaced = client.post(f"{BASE}/conduit-prefs/import?replace=true", json=exported).json()
    assert set(replaced["values"]) == {"app.accent"}


def test_routes_refuse_bad_input(client):
    assert client.put(f"{BASE}/conduit-prefs", json={"changes": {}}).status_code == 400
    assert client.post(f"{BASE}/conduit-prefs/import", json={"kind": "nope"}).status_code == 400
    big = {"installation_id": PHONE, "changes": {"voice.default": "x" * (17 * 1024)}}
    assert client.put(f"{BASE}/conduit-prefs", json=big).status_code == 413


# --- CLI ------------------------------------------------------------------------


def test_cli_exports_and_imports(tmp_path, monkeypatch, capsys):
    cli = _load("conduit_push.cli", ROOT / "cli.py")
    first, second = tmp_path / "first", tmp_path / "second"
    PrefsStore(first).sync({"installation_id": PHONE, "changes": {"app.accent": "violet"}})
    file = tmp_path / "prefs.json"

    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", lambda: first)
    assert cli.dispatch(argparse.Namespace(conduit_push_action="prefs", prefs_action="export",
                                           file=str(file), replace=False)) == 0
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", lambda: second)
    assert cli.dispatch(argparse.Namespace(conduit_push_action="prefs", prefs_action="import",
                                           file=str(file), replace=False)) == 0
    assert PrefsStore(second).view()["values"]["app.accent"]["value"] == "violet"
    assert cli.dispatch(argparse.Namespace(conduit_push_action="prefs", prefs_action="show",
                                           file="", replace=False)) == 0
    assert "app.accent" in capsys.readouterr().out


def test_a_replacing_import_that_drops_keys_moves_the_revision(tmp_path):
    store = _store(tmp_path)
    store.sync({"installation_id": PHONE, "changes": {"app.accent": "violet"}})
    after = store.import_({"kind": "conduit-prefs", "version": 1, "values": {}}, replace=True)
    assert after == {"revision": 2, "values": {}}


def test_an_impossible_time_on_disk_drops_the_entry(tmp_path):
    store = _store(tmp_path)
    store.path.write_text('{"values": {"app.a": {"value": 1, "at": 1e300}, "app.b": {"value": 2, "at": 5}}}')
    assert set(store.view()["values"]) == {"app.b"}


def test_a_non_finite_value_on_disk_drops_the_entry(tmp_path):
    store = _store(tmp_path)
    store.path.write_text('{"values": {"app.a": {"value": NaN, "at": 5}, "app.b": {"value": 2, "at": 5}}}')
    assert set(store.view()["values"]) == {"app.b"}


def test_an_empty_sync_on_a_new_profile_leaves_no_file(tmp_path):
    store = _store(tmp_path)
    store.sync({"installation_id": PHONE, "changes": {}})
    assert not store.path.exists()


@pytest.mark.parametrize("pattern, valid", [("_KEY", "voice.mode"), ("_INSTALLATION", "a" * 8)])
def test_ids_with_a_trailing_newline_are_refused(pattern, valid):
    assert getattr(store_module, pattern).match(valid)
    assert getattr(store_module, pattern).match(valid + "\n") is None
