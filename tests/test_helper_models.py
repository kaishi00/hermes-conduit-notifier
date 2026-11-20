"""Hermes' helper model slots its dashboard leaves out (hermes-conduit#2871,
#2909): the plugin adds Background review and Group router to the dashboard's
slot list, once, after the built-ins, and leaves a slot Hermes lists alone."""

import importlib.util
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_plugin_api():
    spec = importlib.util.spec_from_file_location("conduit_plugin_api_helper_models", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _load_plugin_api()
BUILT_INS = ("vision", "compression", "approval", "mcp", "title_generation", "review", "voice_chat", "curator")


@pytest.fixture
def hermes(monkeypatch):
    """Hermes' dashboard modules as 0.23.2 lays them out: the slot list in
    web_server_config, and the slot list route holding it by name."""
    package = types.ModuleType("hermes_cli")
    package.__path__ = []
    config = types.ModuleType("hermes_cli.web_server_config")
    config._AUX_TASK_SLOTS = BUILT_INS
    package.web_server_config = config
    routers = types.ModuleType("hermes_cli.web_routers")
    routers.__path__ = []
    models = types.ModuleType("hermes_cli.web_routers.models")
    models._AUX_TASK_SLOTS = BUILT_INS
    for name, module in (("hermes_cli", package), ("hermes_cli.web_server_config", config),
                         ("hermes_cli.web_routers", routers), ("hermes_cli.web_routers.models", models)):
        monkeypatch.setitem(sys.modules, name, module)
    return config, models


def test_background_review_and_group_router_are_listed_after_the_built_ins_for_both_routes(hermes):
    config, models = hermes
    assert api.helper_models._add_missing_slots() == ["background_review", "group_router"]
    assert config._AUX_TASK_SLOTS == BUILT_INS + ("background_review", "group_router")
    assert models._AUX_TASK_SLOTS is config._AUX_TASK_SLOTS


def test_it_is_added_once(hermes):
    config, _models = hermes
    api.helper_models._add_missing_slots()
    assert api.helper_models._add_missing_slots() == []
    assert config._AUX_TASK_SLOTS.count("background_review") == 1
    assert config._AUX_TASK_SLOTS.count("group_router") == 1


def test_a_hermes_that_lists_one_gets_only_the_other(hermes):
    config, models = hermes
    own = BUILT_INS + ("group_router",)
    config._AUX_TASK_SLOTS = models._AUX_TASK_SLOTS = own
    assert api.helper_models._add_missing_slots() == ["background_review"]
    assert config._AUX_TASK_SLOTS == own + ("background_review",)


def test_a_hermes_that_lists_it_keeps_its_own_list(hermes):
    config, models = hermes
    own = ("vision", "background_review", "group_router", "curator")
    config._AUX_TASK_SLOTS = models._AUX_TASK_SLOTS = own
    assert api.helper_models._add_missing_slots() == []
    assert config._AUX_TASK_SLOTS is own and models._AUX_TASK_SLOTS is own


def test_a_route_module_holding_another_list_is_left_alone(hermes):
    config, models = hermes
    models._AUX_TASK_SLOTS = ("vision",)
    api.helper_models._add_missing_slots()
    assert "background_review" in config._AUX_TASK_SLOTS
    assert models._AUX_TASK_SLOTS == ("vision",)


def test_a_hermes_without_the_list_is_left_alone(monkeypatch):
    package = types.ModuleType("hermes_cli")
    package.__path__ = []
    config = types.ModuleType("hermes_cli.web_server_config")
    package.web_server_config = config
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server_config", config)
    assert api.helper_models._add_missing_slots() == []
    assert not hasattr(config, "_AUX_TASK_SLOTS")
