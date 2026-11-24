"""Clearing a Hermes speech setting (hermes-conduit issue 3155): the keys are
taken out of the config as written, under the dashboard's config lock."""

import importlib.util
import pathlib
import sys
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"

_spec = importlib.util.spec_from_file_location("conduit_plugin_api_config_unset", ROOT / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(api)


class Lock:
    def __init__(self):
        self.held = False
        self.entered = 0

    def __enter__(self):
        self.held = True
        self.entered += 1

    def __exit__(self, *exc):
        self.held = False


@pytest.fixture
def hermes(monkeypatch):
    """Hermes' config module holding one file, and the dashboard's lock."""
    lock = Lock()
    config = types.ModuleType("hermes_cli.config")
    config.file = {"model": {"default": "m"},
                   "tts": {"provider": "openai", "use_gateway": True, "openai": {"voice": "nova"}},
                   "stt": {"local": {"model": "base", "language": "en"}}}
    config.saves = []
    config.read_raw_config = lambda: __import__("copy").deepcopy(config.file)

    def save_config(data):
        assert lock.held, "saved outside the dashboard's config lock"
        config.saves.append(data)
        config.file = data

    config.save_config = save_config
    package = types.ModuleType("hermes_cli")
    package.__path__ = []
    package.config = config
    web_server = types.ModuleType("hermes_cli.web_server")
    web_server._CONFIG_MUTATION_LOCK = lock
    for name, module in (("hermes_cli", package), ("hermes_cli.config", config),
                         ("hermes_cli.web_server", web_server)):
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(api.core, "_profile_scope", lambda profile: __import__("contextlib").nullcontext())
    config.lock = lock
    return config


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


def test_keys_are_taken_out_with_the_sections_they_leave_empty(hermes, client):
    answer = client.post(f"{BASE}/config/unset", json={"keys": ["tts.openai.voice", "tts.use_gateway",
                                                                "stt.local.model", "tts.openai.voice"]})
    assert answer.status_code == 200, answer.text
    assert answer.json() == {"ok": True, "removed": ["tts.openai.voice", "tts.use_gateway", "stt.local.model"]}
    assert answer.headers["cache-control"] == "no-store"
    assert hermes.file == {"model": {"default": "m"}, "tts": {"provider": "openai"},
                           "stt": {"local": {"language": "en"}}}
    assert len(hermes.saves) == 1


def test_a_key_that_isnt_set_writes_nothing(hermes, client):
    answer = client.post(f"{BASE}/config/unset", json={"keys": ["tts.edge.voice", "stt.provider.model",
                                                                "tts.provider.x"]})
    assert answer.json() == {"ok": True, "removed": []}
    assert hermes.saves == []


def test_a_whole_section_can_go_and_the_last_key_takes_its_parents(hermes, client):
    answer = client.post(f"{BASE}/config/unset", json={"keys": ["stt.local.model", "stt.local.language"]})
    assert answer.json()["removed"] == ["stt.local.model", "stt.local.language"]
    assert "stt" not in hermes.file


@pytest.mark.parametrize("keys", [None, [], "tts.openai.voice", ["model.default"], ["tts"], ["tts..voice"],
                                  ["tts.openai.voi ce"], [7], ["voice.speech_server.url"],
                                  [f"tts.k{i}" for i in range(21)]])
def test_other_keys_are_refused_and_nothing_is_written(hermes, client, keys):
    answer = client.post(f"{BASE}/config/unset", json={"keys": keys})
    assert answer.status_code == 400, answer.text
    assert hermes.saves == [] and hermes.lock.entered == 0


def test_a_managed_hermes_refuses(hermes, client):
    hermes.is_managed = lambda: True
    answer = client.post(f"{BASE}/config/unset", json={"keys": ["tts.openai.voice"]})
    assert answer.status_code == 409
    assert hermes.saves == []


def test_a_key_and_the_section_it_empties_are_both_listed(hermes, client):
    answer = client.post(f"{BASE}/config/unset", json={"keys": ["tts.openai.voice", "tts.openai"]})
    assert answer.json()["removed"] == ["tts.openai.voice", "tts.openai"]
    assert hermes.file["tts"] == {"provider": "openai", "use_gateway": True}


def test_the_strict_read_is_what_gets_changed_and_saved(hermes, client):
    hermes.require_readable_config_before_write = lambda: {"tts": {"edge": {"voice": "v"}, "provider": "edge"}}
    hermes.read_raw_config = None
    answer = client.post(f"{BASE}/config/unset", json={"keys": ["tts.edge.voice"]})
    assert answer.json()["removed"] == ["tts.edge.voice"]
    assert hermes.saves == [{"tts": {"provider": "edge"}}]


def test_the_strict_read_is_used_when_hermes_has_one(hermes, client):
    def strict():
        raise RuntimeError("config.yaml can't be parsed")

    hermes.require_readable_config_before_write = strict
    answer = client.post(f"{BASE}/config/unset", json={"keys": ["tts.openai.voice"]})
    assert answer.status_code == 500
    assert hermes.saves == []


def test_it_runs_in_the_profile_named(hermes, client, monkeypatch):
    scoped = []

    class Scope:
        def __init__(self, profile):
            self.profile = profile

        def __enter__(self):
            scoped.append(self.profile)

        def __exit__(self, *exc):
            scoped.append(None)

    def save_config(data):
        assert scoped[-1] == "second"
        hermes.saves.append(data)

    hermes.save_config = save_config
    monkeypatch.setattr(api.core, "_profile_scope", Scope)
    answer = client.post(f"{BASE}/config/unset?profile=second", json={"keys": ["tts.openai.voice"]})
    assert answer.status_code == 200 and len(hermes.saves) == 1
