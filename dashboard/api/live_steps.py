"""Live steps: what each running chat of a profile is doing now, for Conduit's
bots lists (Eric, 2026-11-26). The plugin's hooks note each running turn's
step (thinking, a tool's category, waiting on an approval or an answer) in the
profile's conduit-live-steps.json (live_steps.py in the plugin's folder); this
reads it. An app shows a step only for a chat Hermes' live registry
(``session.active_list``) also says is working.
"""

from __future__ import annotations

import importlib.util
import os
import threading
from typing import Any, Dict, Optional

from fastapi import HTTPException, Response

from . import core

router = core.router

# Its own bucket: an app reads it every few seconds per bot while one works.
LIVE_STEPS_LIMIT = 240
LIVE_STEPS_WINDOW_S = 60.0
_live_steps_limiter = core._MintLimiter(LIVE_STEPS_LIMIT, LIVE_STEPS_WINDOW_S,
                                        message="Too many live-step reads; try again shortly")

_module: Any = None
_module_lock = threading.Lock()


def _live_steps() -> Any:
    """live_steps.py in the plugin's folder, loaded once."""
    global _module
    with _module_lock:
        if _module is None:
            path = os.path.join(core._plugin_root(), "live_steps.py")
            spec = importlib.util.spec_from_file_location("conduit_push_dashboard_live_steps", path)
            if spec is None or spec.loader is None:
                raise RuntimeError("This plugin install is missing live_steps.py; update the plugin")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _module = module
        return _module


def live_steps_list() -> Dict[str, Any]:
    return {"steps": _live_steps().read(core._calls_home())}


@router.get("/live-steps")
async def get_live_steps(response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    """``steps``: the profile's running chats by session id, each with its ``phase`` (thinking, tool,
    approval or input), ``tool`` (the tool's category, as the Lock Screen names it, for a tool) and
    ``since`` (when the step began, Unix seconds)."""
    response.headers["Cache-Control"] = "no-store"
    try:
        return {"ok": True, **(await core._run_scoped(profile, core._limited(_live_steps_limiter, profile, live_steps_list)))}
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise core._unexpected("live steps", exc, feature="Bots")
