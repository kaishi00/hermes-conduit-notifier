"""A chat's model pick keeps its nested gateway_runtime in step (api/session_route.py)."""
import importlib.util
import os
import pathlib
import sys
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]

if "hermes_constants" not in sys.modules:
    _hermes_constants = types.ModuleType("hermes_constants")
    _hermes_constants.get_hermes_home = lambda: pathlib.Path(os.environ["HERMES_HOME"])
    sys.modules["hermes_constants"] = _hermes_constants


def _load_plugin_api():
    spec = importlib.util.spec_from_file_location("conduit_plugin_api_session_route", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _load_plugin_api()
route = api.session_route


class Agent:
    def __init__(self, model="deepseek-v4.1-flash", provider="opencode-go",
                 base_url="https://opencode.ai/zen/go/v1", fallback=False):
        self.model, self.provider, self.base_url, self.api_mode = model, provider, base_url, ""
        self._fallback_activated = fallback


def _hermes_runtime_model_config(agent, existing=None):
    """v0.23.0's merge, shortened: the live route over the row's config,
    falsy attributes deleting their key, the nested copy untouched."""
    config = dict(existing or {})
    for key in ("model", "provider", "base_url", "api_mode"):
        value = getattr(agent, key, "")
        if value:
            config[key] = value
        else:
            config.pop(key, None)
    return config


@pytest.fixture
def server(monkeypatch):
    module = types.ModuleType("tui_gateway.server")
    module._runtime_model_config = _hermes_runtime_model_config
    monkeypatch.setitem(sys.modules, "tui_gateway.server", module)
    yield module
    route.uninstall()


STALE = {
    "model": "glm-5.3-flash", "provider": "opencode-go", "base_url": "https://opencode.ai/zen/go/v1",
    "composer_override_profile": {"model": "deepseek-v4.1-flash", "provider": "opencode-go"},
    "gateway_runtime": {"provider": "zai", "base_url": "https://api.z.ai/api/coding/paas/v4",
                        "fallback_active": False, "note": "kept"},
}


def test_a_pick_rewrites_a_stale_nested_route(server):
    assert route.ensure_installed()
    config = server._runtime_model_config(Agent(), STALE)
    assert config["provider"] == "opencode-go"
    assert config["gateway_runtime"] == {
        "provider": "opencode-go", "base_url": "https://opencode.ai/zen/go/v1",
        "fallback_active": False, "note": "kept"}
    # The rest of Hermes' answer is its own.
    assert config["composer_override_profile"] == STALE["composer_override_profile"]
    assert config["model"] == "deepseek-v4.1-flash"


def test_a_chat_without_a_nested_route_gets_none(server):
    assert route.ensure_installed()
    config = server._runtime_model_config(Agent(), {"provider": "zai"})
    assert "gateway_runtime" not in config
    assert server._runtime_model_config(Agent(), None) == _hermes_runtime_model_config(Agent(), None)


def test_a_live_route_without_a_provider_drops_the_nested_one(server):
    assert route.ensure_installed()
    config = server._runtime_model_config(Agent(provider="", base_url=""), STALE)
    assert "provider" not in config and "gateway_runtime" not in config


def test_a_fallback_turn_says_so(server):
    assert route.ensure_installed()
    config = server._runtime_model_config(
        Agent(model="glm-5.3-flash", provider="zai", base_url="https://api.z.ai/api/coding/paas/v4", fallback=True),
        STALE)
    assert config["gateway_runtime"]["provider"] == "zai"
    assert config["gateway_runtime"]["fallback_active"] is True


def test_install_is_once_and_follows_a_reload(server):
    assert route.ensure_installed()
    wrapped = server._runtime_model_config
    assert route.ensure_installed()
    assert server._runtime_model_config is wrapped
    # A reload puts Hermes' own back: the next request wraps it again.
    server._runtime_model_config = _hermes_runtime_model_config
    assert route.ensure_installed()
    assert server._runtime_model_config is not _hermes_runtime_model_config
    route.uninstall()
    assert server._runtime_model_config is _hermes_runtime_model_config


def test_nothing_without_a_loaded_server(monkeypatch):
    monkeypatch.delitem(sys.modules, "tui_gateway.server", raising=False)
    assert route.ensure_installed() is False
    assert "tui_gateway.server" not in sys.modules
    server = types.ModuleType("tui_gateway.server")
    monkeypatch.setitem(sys.modules, "tui_gateway.server", server)
    assert route.ensure_installed() is False


def test_a_conduit_request_installs_it(server, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/conduit_push")
    TestClient(app).get("/api/plugins/conduit_push/link-status")
    assert getattr(server._runtime_model_config, route._GUARD_FLAG, False)
