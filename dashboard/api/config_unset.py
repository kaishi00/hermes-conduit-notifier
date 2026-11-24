"""Clearing a Hermes speech setting: taking keys out of a profile's config.yaml
(hermes-conduit issue 3155).

Hermes' ``PUT /api/config`` merges what it's sent over the file, so a key a
save leaves out keeps its old value, and Hermes reads its speech providers'
settings with ``config.get(key, default)``, so a saved ``""`` isn't the
default either. Conduit's "leave blank for the default" fields (a speech
provider's model, voice, language, endpoint) and the stale ``use_gateway`` a
provider pick drops need the key gone, which this route does: it reads the
file as written, takes the keys out with any section that leaves empty, and
saves the rest, under the lock Hermes' own config routes hold.

Only keys under ``stt`` and ``tts`` are taken, the ones Conduit clears; the
rest of the config is Hermes' to change through its own routes.

A ``?profile=`` request for another profile runs in that profile's plugin
host on Hermes 0.22.3+, a process of its own, where the dashboard's lock
isn't held: a dashboard save of the same profile in the same instant could
put a key back. Conduit queues its own saves of a profile, so they don't
overlap.
"""

from __future__ import annotations

import re
import sys
from contextlib import nullcontext
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, Request, Response

from . import core

router = core.router
logger = core.logger

CONFIG_UNSET_SECTIONS = ("stt", "tts")
CONFIG_UNSET_MAX_KEYS = 20
CONFIG_UNSET_MAX_BODY_BYTES = 8 * 1024
CONFIG_UNSET_LIMIT = 30
CONFIG_UNSET_WINDOW_S = 60.0

_KEY_PART = re.compile(r"[A-Za-z0-9_-]{1,64}")
_config_unset_limiter = core._MintLimiter(CONFIG_UNSET_LIMIT, CONFIG_UNSET_WINDOW_S,
                                          message="Too many settings changes; try again shortly")


def _keys(body: Any) -> List[List[str]]:
    """The body's keys as paths, each once. Raises TokenError (400) on one this route doesn't take."""
    keys = body.get("keys") if isinstance(body, dict) else None
    if not isinstance(keys, list) or not keys:
        raise core.TokenError(400, "keys must be a list of config keys")
    if len(keys) > CONFIG_UNSET_MAX_KEYS:
        raise core.TokenError(400, f"At most {CONFIG_UNSET_MAX_KEYS} keys at a time")
    paths: List[List[str]] = []
    for key in keys:
        parts = key.split(".") if isinstance(key, str) else []
        if len(parts) < 2 or parts[0] not in CONFIG_UNSET_SECTIONS or not all(_KEY_PART.fullmatch(p) for p in parts):
            raise core.TokenError(400, "Only settings under stt and tts can be cleared here")
        if parts not in paths:
            paths.append(parts)
    return paths


def _take(config: Dict[str, Any], path: List[str]) -> bool:
    """Takes ``path`` out of ``config``, with each section that leaves empty. Whether it was there."""
    parents = [config]
    for part in path[:-1]:
        child = parents[-1].get(part)
        if not isinstance(child, dict):
            return False
        parents.append(child)
    if path[-1] not in parents[-1]:
        return False
    del parents[-1][path[-1]]
    for depth in range(len(parents) - 1, 0, -1):
        if parents[depth]:
            break
        del parents[depth - 1][path[depth - 1]]
    return True


def _dashboard_config_lock() -> Any:
    """The lock Hermes' config routes hold across a read and its save; none outside the dashboard's process."""
    web_server = sys.modules.get("hermes_cli.web_server")
    lock = getattr(web_server, "_CONFIG_MUTATION_LOCK", None)
    if hasattr(lock, "__enter__"):
        return lock
    if web_server is not None:
        # A Hermes that renamed it: the save still has save_config's own lock, not the routes'.
        logger.warning("conduit_push: Hermes' dashboard has no _CONFIG_MUTATION_LOCK; clearing config keys without it")
    return nullcontext()


def unset_config_keys(body: Any) -> Dict[str, Any]:
    """Takes the body's keys out of the scoped profile's config.yaml. ``removed`` lists the ones that were there;
    with none, the file isn't written."""
    paths = _keys(body)
    try:
        from hermes_cli import config as hermes_config
    except ImportError:
        raise core.TokenError(501, "This Hermes can't change its config from the plugin; update Hermes")
    if getattr(hermes_config, "is_managed", lambda: False)():
        raise core.TokenError(409, "This Hermes is managed, so its settings can't be changed from Conduit")
    # The strict read refuses a file it can't parse, where the plain one answers {}.
    read = getattr(hermes_config, "require_readable_config_before_write", None) or hermes_config.read_raw_config
    with _dashboard_config_lock():
        config = read()
        if not isinstance(config, dict):
            raise core.TokenError(409, "This profile's config.yaml isn't a mapping")
        removed = [".".join(path) for path in paths if _take(config, path)]
        if removed:
            hermes_config.save_config(config)
    return {"removed": removed}


@router.post("/config/unset")
async def post_config_unset(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    body = await core._read_json_body(request, CONFIG_UNSET_MAX_BODY_BYTES)
    try:
        return {"ok": True, **(await core._run_scoped(
            profile, core._limited(_config_unset_limiter, profile, lambda: unset_config_keys(body))))}
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise core._unexpected("unset", exc, feature="Hermes config")
