"""Image and video generation models: the active backend's models from Hermes'
catalog, and a pick saved where that backend reads it."""

import contextlib
import copy
import importlib.util
import pathlib
import sys
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"

_spec = importlib.util.spec_from_file_location("conduit_plugin_api_media_models", ROOT / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(api)

CATALOGS = {
    "openai": ({"gpt-image-2-medium": {"display": "GPT Image 2 (medium)", "speed": "~20s", "price": "$0.04"},
                "gpt-image-2-high": {"display": "GPT Image 2 (high)"}}, "gpt-image-2-medium"),
    "xai": ({"grok-imagine-image": {"display": "Grok Imagine"}, "grok-imagine-image-pro": {}}, "grok-imagine-image"),
    "fal": ({"flux-2-pro": {"display": "FLUX 2 Pro"}, "z-image-turbo": {}}, "flux-2-pro"),
}


class Lock:
    def __init__(self):
        self.held = False

    def __enter__(self):
        self.held = True

    def __exit__(self, *exc):
        self.held = False


@pytest.fixture
def hermes(monkeypatch):
    """Hermes' config module holding one file, its toolsets routes' helpers, and the dashboard's lock."""
    lock = Lock()
    config = types.ModuleType("hermes_cli.config")
    config.file = {"model": {"default": "m"}, "image_gen": {"provider": "openai"}}
    config.env = {}
    config.saves = []
    config.read_raw_config = lambda: copy.deepcopy(config.file)
    config.load_config = lambda: copy.deepcopy(config.file)
    config.get_env_value = lambda key: config.env.get(key)

    def save_config(data):
        assert lock.held, "saved outside the dashboard's config lock"
        config.saves.append(copy.deepcopy(data))
        config.file = data

    config.save_config = save_config
    tools = types.ModuleType("hermes_cli.web_routers.tools")

    def find_row(section, cfg, provider):
        assert provider is None
        name = (cfg.get(section) or {}).get("provider")
        return {"name": f"{name} row", "image_gen_plugin_name": name} if name else None

    tools._find_toolset_provider_row = find_row
    tools._resolve_toolset_model_plugin = lambda section, row: row.get("image_gen_plugin_name")
    tools._toolset_model_catalog = lambda section, plugin, cfg: copy.deepcopy(CATALOGS.get(plugin, ({}, None)))
    package = types.ModuleType("hermes_cli")
    package.__path__ = []
    package.config = config
    routers = types.ModuleType("hermes_cli.web_routers")
    routers.__path__ = []
    routers.tools = tools
    package.web_routers = routers
    web_server = types.ModuleType("hermes_cli.web_server")
    web_server._CONFIG_MUTATION_LOCK = lock
    for name, module in (("hermes_cli", package), ("hermes_cli.config", config),
                         ("hermes_cli.web_routers", routers), ("hermes_cli.web_routers.tools", tools),
                         ("hermes_cli.web_server", web_server)):
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(api.core, "_profile_scope", lambda profile: contextlib.nullcontext())
    config.tools = tools
    return config


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


def test_the_active_backends_models_are_listed_with_the_default_in_use(hermes, client):
    answer = client.get(f"{BASE}/media-models/image_gen")
    assert answer.status_code == 200, answer.text
    assert answer.headers["cache-control"] == "no-store"
    body = answer.json()
    assert body["plugin"] == "openai" and body["provider"] == "openai row" and body["has_models"]
    assert body["current"] == body["default"] == "gpt-image-2-medium"
    assert body["models"][0] == {"id": "gpt-image-2-medium", "display": "GPT Image 2 (medium)", "speed": "~20s",
                                 "strengths": "", "price": "$0.04"}
    assert body["models"][1]["display"] == "GPT Image 2 (high)"
    assert body["override"] is None


def test_the_backends_own_key_is_what_it_uses_over_the_sections(hermes, client):
    hermes.file["image_gen"].update(model="gpt-image-2-medium", openai={"model": "gpt-image-2-high"})
    assert client.get(f"{BASE}/media-models/image_gen").json()["current"] == "gpt-image-2-high"


def test_a_saved_model_from_another_backend_reads_as_the_default(hermes, client):
    hermes.file["image_gen"]["model"] = "flux-2-pro"
    assert client.get(f"{BASE}/media-models/image_gen").json()["current"] == "gpt-image-2-medium"


def test_xai_reads_only_its_own_key(hermes, client):
    hermes.file["image_gen"] = {"provider": "xai", "model": "grok-imagine-image-pro"}
    assert client.get(f"{BASE}/media-models/image_gen").json()["current"] == "grok-imagine-image"


def test_a_pick_for_xai_goes_to_its_own_key_too(hermes, client):
    hermes.file["image_gen"] = {"provider": "xai"}
    answer = client.put(f"{BASE}/media-models/image_gen", json={"model": "grok-imagine-image-pro"})
    assert answer.status_code == 200, answer.text
    assert answer.json() == {"ok": True, "kind": "image_gen", "model": "grok-imagine-image-pro", "plugin": "xai",
                             "override": None}
    assert hermes.file["image_gen"] == {"provider": "xai", "model": "grok-imagine-image-pro",
                                        "xai": {"model": "grok-imagine-image-pro"}}
    assert client.get(f"{BASE}/media-models/image_gen").json()["current"] == "grok-imagine-image-pro"


def test_a_pick_writes_the_sections_key_and_leaves_the_rest(hermes, client):
    answer = client.put(f"{BASE}/media-models/image_gen", json={"model": "gpt-image-2-high"})
    assert answer.status_code == 200, answer.text
    assert hermes.file == {"model": {"default": "m"}, "image_gen": {"provider": "openai", "model": "gpt-image-2-high"}}
    assert len(hermes.saves) == 1


def test_an_older_pick_in_the_backends_own_key_is_replaced(hermes, client):
    hermes.file["image_gen"]["openai"] = {"model": "gpt-image-2-medium", "quality": "high"}
    client.put(f"{BASE}/media-models/image_gen", json={"model": "gpt-image-2-high"})
    assert hermes.file["image_gen"]["openai"] == {"model": "gpt-image-2-high", "quality": "high"}


def test_video_has_its_own_section(hermes, client):
    hermes.file["video_gen"] = {"provider": "fal"}
    answer = client.put(f"{BASE}/media-models/video_gen", json={"model": "z-image-turbo"})
    assert answer.status_code == 200, answer.text
    assert hermes.file["video_gen"] == {"provider": "fal", "model": "z-image-turbo"}
    assert hermes.file["image_gen"] == {"provider": "openai"}


def test_an_env_var_overriding_the_pick_is_named(hermes, client):
    hermes.env["OPENAI_IMAGE_MODEL"] = "gpt-image-2-high"
    assert client.get(f"{BASE}/media-models/image_gen").json()["override"] == {
        "env": "OPENAI_IMAGE_MODEL", "model": "gpt-image-2-high"}
    answer = client.put(f"{BASE}/media-models/image_gen", json={"model": "gpt-image-2-medium"})
    assert answer.json()["override"]["env"] == "OPENAI_IMAGE_MODEL"


def test_fals_env_var_never_overrides_a_pick(hermes, client):
    hermes.file["image_gen"] = {"provider": "fal"}
    hermes.env["FAL_IMAGE_MODEL"] = "z-image-turbo"
    assert client.get(f"{BASE}/media-models/image_gen").json()["override"] is None


def test_no_backend_lists_no_models_and_refuses_a_pick(hermes, client):
    hermes.file["image_gen"] = {}
    body = client.get(f"{BASE}/media-models/image_gen").json()
    assert body["has_models"] is False and body["models"] == [] and body["current"] is None
    answer = client.put(f"{BASE}/media-models/image_gen", json={"model": "gpt-image-2-high"})
    assert answer.status_code == 409
    assert hermes.saves == []


@pytest.mark.parametrize("body", [{}, {"model": ""}, {"model": 7}, {"model": "x" * 201}, ["gpt-image-2-high"]])
def test_a_missing_model_is_refused(hermes, client, body):
    assert client.put(f"{BASE}/media-models/image_gen", json=body).status_code == 400
    assert hermes.saves == []


def test_a_model_the_backend_doesnt_list_is_refused(hermes, client):
    answer = client.put(f"{BASE}/media-models/image_gen", json={"model": "flux-2-pro"})
    assert answer.status_code == 400 and "openai" in answer.json()["detail"]
    assert hermes.saves == []


def test_other_sections_are_not_found(hermes, client):
    assert client.get(f"{BASE}/media-models/tts").status_code == 404
    assert client.put(f"{BASE}/media-models/tts", json={"model": "x"}).status_code == 404


def test_a_managed_hermes_refuses(hermes, client):
    hermes.is_managed = lambda: True
    assert client.put(f"{BASE}/media-models/image_gen", json={"model": "gpt-image-2-high"}).status_code == 409
    assert hermes.saves == []


def test_a_hermes_without_the_catalog_helpers_says_to_update(hermes, client):
    del hermes.tools._toolset_model_catalog
    answer = client.get(f"{BASE}/media-models/image_gen")
    assert answer.status_code == 501 and "update Hermes" in answer.json()["detail"]


def test_it_runs_in_the_profile_named(hermes, client, monkeypatch):
    scoped = []

    @contextlib.contextmanager
    def scope(profile):
        scoped.append(profile)
        yield

    monkeypatch.setattr(api.core, "_profile_scope", scope)
    assert client.put(f"{BASE}/media-models/image_gen?profile=second",
                      json={"model": "gpt-image-2-high"}).status_code == 200
    assert scoped == ["second"]


def test_xais_video_backend_reads_only_the_sections_key(hermes, client):
    CATALOGS["xai-video"] = ({"grok-imagine-video": {}, "grok-imagine-video-1.5": {}}, "grok-imagine-video")
    hermes.tools._toolset_model_catalog = lambda section, plugin, cfg: copy.deepcopy(
        CATALOGS["xai-video" if section == "video_gen" and plugin == "xai" else plugin])
    hermes.file["video_gen"] = {"provider": "xai", "model": "grok-imagine-video",
                                "xai": {"model": "an-old-pick"}}
    assert client.get(f"{BASE}/media-models/video_gen").json()["current"] == "grok-imagine-video"
    answer = client.put(f"{BASE}/media-models/video_gen", json={"model": "grok-imagine-video-1.5"})
    assert answer.status_code == 200, answer.text
    assert hermes.file["video_gen"]["model"] == "grok-imagine-video-1.5"
    assert hermes.file["video_gen"]["xai"] == {"model": "an-old-pick"}, "A key it never reads is left as it was"


def test_an_over_long_model_says_so(hermes, client):
    answer = client.put(f"{BASE}/media-models/image_gen", json={"model": "x" * 201})
    assert answer.status_code == 400 and "at most 200" in answer.json()["detail"]


def test_a_backend_with_an_empty_catalog_refuses_a_pick_as_no_backend(hermes, client):
    hermes.file["image_gen"] = {"provider": "krea"}
    answer = client.put(f"{BASE}/media-models/image_gen", json={"model": "anything"})
    assert answer.status_code == 409 and "image backend" in answer.json()["detail"]
