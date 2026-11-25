"""Image and video generation models: which model a profile's image or video
backend uses, and picking another (hermes-conduit, Image and video generation
models).

Hermes keeps the backend in ``image_gen.provider`` (``video_gen.provider``) and
the model in ``image_gen.model``, and its dashboard lists the active backend's
models and saves a pick (``/api/tools/toolsets/<name>/models`` and ``/model``).
That save writes ``image_gen.model`` alone, but most backends read a key of
their own first, ``image_gen.<backend>.model``, and xAI's and DeepInfra's image
backends read only theirs, so a pick there changed nothing. Most also take an
env var over the config (``OPENAI_IMAGE_MODEL`` and the like), which the
dashboard doesn't mention.

These routes list the same models from Hermes' own catalog and save a pick
where the backend reads it: ``<section>.model`` always, and the backend's own
key when it reads only that one or already holds one (so an older pick there
can't shadow the new one). They name the env var that overrides the pick, when
the profile sets one, so Conduit can say why a pick doesn't take.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from fastapi import HTTPException, Request, Response

from . import config_unset, core

router = core.router
logger = core.logger

MEDIA_SECTIONS = ("image_gen", "video_gen")
MEDIA_MODEL_MAX_BODY_BYTES = 4 * 1024
MEDIA_MODEL_MAX_ID = 200
MEDIA_MODEL_LIMIT = 30
MEDIA_MODEL_WINDOW_S = 60.0

# Which keys a backend reads, where it isn't both (its own first, then the
# section's). Hermes' image backends for xAI and DeepInfra never read
# image_gen.model; its xAI video backend reads only video_gen.model.
OWN_KEY_ONLY = {("image_gen", "xai"), ("image_gen", "deepinfra")}
SECTION_KEY_ONLY = {("video_gen", "xai")}

# The env var a backend takes over its config, from Hermes' backends. FAL's
# image backend reads image_gen.model first, so its FAL_IMAGE_MODEL never
# overrides a pick.
ENV_OVERRIDES = {
    "image_gen": {
        "openai": "OPENAI_IMAGE_MODEL",
        "openai-codex": "OPENAI_IMAGE_MODEL",
        "openrouter": "OPENROUTER_IMAGE_MODEL",
        "xai": "XAI_IMAGE_MODEL",
        "krea": "KREA_IMAGE_MODEL",
        "deepinfra": "DEEPINFRA_IMAGE_MODEL",
        "meta-ai": "META_IMAGE_MODEL",
    },
    "video_gen": {
        "fal": "FAL_VIDEO_MODEL",
    },
}

_media_model_limiter = core._MintLimiter(MEDIA_MODEL_LIMIT, MEDIA_MODEL_WINDOW_S,
                                         message="Too many model changes; try again shortly")


def _section(kind: str) -> str:
    if kind not in MEDIA_SECTIONS:
        raise core.TokenError(404, f"No model setting for {kind!r}")
    return kind


def _reads(section: str, plugin: str) -> Tuple[bool, bool]:
    """Whether the backend reads (its own key, the section's key)."""
    if (section, plugin) in OWN_KEY_ONLY:
        return True, False
    if (section, plugin) in SECTION_KEY_ONLY:
        return False, True
    return True, True


def _hermes():
    """Hermes' config module and its toolsets routes' helpers. 501 on a Hermes without them."""
    try:
        from hermes_cli import config as hermes_config
        from hermes_cli.web_routers import tools as hermes_tools
    except ImportError as exc:
        # Only a Hermes without the modules; a broken import inside one is the host's error.
        if exc.name not in ("hermes_cli", "hermes_cli.config", "hermes_cli.web_routers",
                            "hermes_cli.web_routers.tools"):
            raise
        raise core.TokenError(501, "This Hermes can't list its image or video models; update Hermes")
    for name in ("_find_toolset_provider_row", "_resolve_toolset_model_plugin", "_toolset_model_catalog"):
        if not callable(getattr(hermes_tools, name, None)):
            raise core.TokenError(501, "This Hermes can't list its image or video models; update Hermes")
    return hermes_config, hermes_tools


def _backend(section: str, config: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str], Dict, Any]:
    """The active backend's picker row, its plugin name, its catalog and its default."""
    _config, tools = _hermes()
    row = tools._find_toolset_provider_row(section, config, None)
    plugin = tools._resolve_toolset_model_plugin(section, row) if row else None
    if not plugin:
        return row, None, {}, None
    catalog, default = tools._toolset_model_catalog(section, plugin, config)
    return row, plugin, catalog if isinstance(catalog, dict) else {}, default


def _text(value: Any) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _saved(section: str, plugin: str, config: Dict[str, Any], catalog: Dict) -> Optional[str]:
    """The catalog model the backend reads from the config, the way it reads it. Catalog-scoped: an id the
    backend no longer lists is passed over, as the backends pass over one they don't know."""
    block = config.get(section) if isinstance(config.get(section), dict) else {}
    own_block = block.get(plugin) if isinstance(block.get(plugin), dict) else {}
    reads_own, reads_section = _reads(section, plugin)
    candidates = ([_text(own_block.get("model"))] if reads_own else []) + \
                 ([_text(block.get("model"))] if reads_section else [])
    return next((c for c in candidates if c and c in catalog), None)


def _override(section: str, plugin: str) -> Optional[Dict[str, str]]:
    """The env var the profile sets that the backend takes over its config."""
    var = ENV_OVERRIDES.get(section, {}).get(plugin)
    if not var:
        return None
    hermes_config, _tools = _hermes()
    get_env_value = getattr(hermes_config, "get_env_value", None)
    value = _text(get_env_value(var)) if callable(get_env_value) else None
    return {"env": var, "model": value} if value else None


def media_models(kind: str) -> Dict[str, Any]:
    section = _section(kind)
    hermes_config, _tools = _hermes()
    config = hermes_config.load_config()
    row, plugin, catalog, default = _backend(section, config)
    if not plugin or not catalog:
        return {"kind": section, "has_models": False, "provider": (row or {}).get("name"), "plugin": plugin,
                "models": [], "current": None, "default": None, "override": None}
    current = _saved(section, plugin, config, catalog) or (default if default in catalog else None)
    models = [{"id": model_id, "display": (meta or {}).get("display", model_id),
               "speed": (meta or {}).get("speed", ""), "strengths": (meta or {}).get("strengths", ""),
               "price": (meta or {}).get("price", "")}
              for model_id, meta in catalog.items()]
    return {"kind": section, "has_models": True, "provider": row.get("name"), "plugin": plugin,
            "models": models, "current": current, "default": default if default in catalog else None,
            "override": _override(section, plugin)}


def set_media_model(kind: str, body: Any) -> Dict[str, Any]:
    section = _section(kind)
    raw = body.get("model") if isinstance(body, dict) else None
    if raw is not None and not isinstance(raw, str):
        raise core.TokenError(400, "model must be text")
    model = _text(raw)
    if not model:
        raise core.TokenError(400, "model is required")
    if len(model) > MEDIA_MODEL_MAX_ID:
        raise core.TokenError(400, f"model is at most {MEDIA_MODEL_MAX_ID} characters")
    hermes_config, _tools = _hermes()
    if getattr(hermes_config, "is_managed", lambda: False)():
        raise core.TokenError(409, "This Hermes is managed, so its settings can't be changed from Conduit")
    read = getattr(hermes_config, "require_readable_config_before_write", None) or hermes_config.read_raw_config
    # The dashboard's config lock, so a save here and one of Hermes' own don't interleave.
    with config_unset._dashboard_config_lock():
        _row, plugin, catalog, _default = _backend(section, hermes_config.load_config())
        if not plugin or not catalog:
            raise core.TokenError(409, f"No {section.replace('_gen', '')} backend with models is set up for this profile")
        if model not in catalog:
            raise core.TokenError(400, f"{plugin} has no model {model!r}")
        config = read()
        if not isinstance(config, dict):
            raise core.TokenError(409, "This profile's config.yaml isn't a mapping")
        block = config.get(section)
        if not isinstance(block, dict):
            block = config[section] = {}
        block["model"] = model
        reads_own, reads_section = _reads(section, plugin)
        own_block = block.get(plugin)
        if reads_own and (not reads_section or (isinstance(own_block, dict) and "model" in own_block)):
            if not isinstance(own_block, dict):
                own_block = block[plugin] = {}
            own_block["model"] = model
        hermes_config.save_config(config)
    return {"kind": section, "model": model, "plugin": plugin, "override": _override(section, plugin)}


def _refusal(exc: core.TokenError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})


@router.get("/media-models/{kind}")
async def get_media_models(kind: str, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await core._run_scoped(profile, lambda: media_models(kind))
    except core.TokenError as exc:
        raise _refusal(exc)
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise core._unexpected("list", exc, feature="Image or video models")


@router.put("/media-models/{kind}")
async def put_media_model(kind: str, request: Request, response: Response,
                          profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    body = await core._read_json_body(request, MEDIA_MODEL_MAX_BODY_BYTES)
    try:
        return {"ok": True, **(await core._run_scoped(
            profile, core._limited(_media_model_limiter, profile, lambda: set_media_model(kind, body))))}
    except core.TokenError as exc:
        raise _refusal(exc)
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise core._unexpected("save", exc, feature="Image or video models")
