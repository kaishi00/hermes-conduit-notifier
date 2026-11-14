"""A chat's model pick sticks: the route a TUI chat persists keeps its nested
``gateway_runtime`` in step (hermes-conduit FEATURE_MAP "The Model sheet's
settings").

Hermes keeps two copies of a chat's route in its ``model_config``: the
top-level ``provider``/``base_url``/``api_mode`` the TUI gateway writes
(tui_gateway/agent_factory.py ``_persist_live_session_runtime``, through
``_runtime_model_config``), and a nested ``gateway_runtime`` the messaging
gateway writes after its turns (gateway/run_turn_prepare.py
``_sync_session_model_from_agent``) and the CLI's ``/model`` writes with the
top-level keys. The reader every rebuild and resume goes through
(hermes_state_gateway.py ``session_gateway_runtime``) takes the nested copy
first. The TUI's persist never touches it, so once a chat has one, a model
picked in Conduit (``config.set model``) is saved with its new provider at
the top and the old provider below, and the next rebuild pairs the new model
with the old provider. Furina's Bot Chat on Eric's host (2026-11-14): a
fallback turn left ``gateway_runtime`` on zai; every pick after it rebuilt
as "<pick> on zai", zai refused the model, and the fallback chain landed on
glm-5.3-flash again, so the picker snapped back.

Until Hermes keeps them together, the TUI's persist writes its live route
into ``gateway_runtime`` too when the chat has one, the way the CLI's
``/model`` does, so both copies name the route the chat last ran. A chat
without one is left as Hermes writes it.

The TUI gateway publishes its split modules' functions onto
``tui_gateway.server`` and calls them there, so the wrapper goes on the
server's copy. Never imported here: a gateway this plugin imported would be
a private copy no client uses. Every Conduit request installs it if the
server is loaded and doesn't have it (a reload replaces the server's
functions), which costs a dict lookup once it's on.
"""

from __future__ import annotations

import functools
import sys
import threading
from typing import Any, Callable, Optional

from . import core

SERVER = "tui_gateway.server"
METHOD = "_runtime_model_config"
NESTED = "gateway_runtime"
# The route keys both copies hold (what session_gateway_runtime reads).
ROUTE_KEYS = ("provider", "base_url", "api_mode")
_GUARD_FLAG = "__conduit_session_route__"

_lock = threading.Lock()
_logged = False


def aligned(config: Any, agent: Any) -> Any:
    """``config`` with its nested ``gateway_runtime`` naming the top-level
    route, when it has one. The rest of the nested copy stays, but
    ``fallback_active`` says whether the live agent runs on a fallback."""
    if not isinstance(config, dict):
        return config
    nested = config.get(NESTED)
    if not isinstance(nested, dict):
        return config
    route = {key: config[key] for key in ROUTE_KEYS if config.get(key)}
    if not route.get("provider"):
        # Nothing to name: without the nested copy the reader falls back to
        # the row's billing provider, as for a chat that never had one.
        config.pop(NESTED, None)
        return config
    kept = {key: value for key, value in nested.items() if key not in ROUTE_KEYS and key != "fallback_active"}
    config[NESTED] = {**kept, **route, "fallback_active": bool(getattr(agent, "_fallback_activated", False))}
    return config


def _guard(own: Callable[..., Any]) -> Callable[..., Any]:
    def runtime_model_config(agent: Any, *args: Any, **kwargs: Any) -> Any:
        config = own(agent, *args, **kwargs)
        try:
            return aligned(config, agent)
        except Exception:  # noqa: BLE001 — Hermes' own answer stands
            core.logger.debug("Conduit: couldn't align a chat's gateway_runtime", exc_info=True)
            return config

    functools.update_wrapper(runtime_model_config, own)
    setattr(runtime_model_config, _GUARD_FLAG, True)
    return runtime_model_config


def ensure_installed() -> bool:
    """Puts the wrapper on the loaded server's ``_runtime_model_config``.
    False while no server is loaded here, or it has no such function (that
    Hermes persists the route its own way)."""
    global _logged
    server = sys.modules.get(SERVER)
    current: Optional[Callable[..., Any]] = getattr(server, METHOD, None) if server is not None else None
    if not callable(current):
        return False
    if getattr(current, _GUARD_FLAG, False):
        return True
    with _lock:
        current = getattr(server, METHOD, None)
        if getattr(current, _GUARD_FLAG, False):
            return True
        if not callable(current):
            return False
        setattr(server, METHOD, _guard(current))
        if not _logged:
            _logged = True
            core.logger.info("Conduit: a chat's model pick now keeps its gateway_runtime in step")
    return True


def uninstall() -> None:
    """Puts the server's own function back (tests)."""
    global _logged
    _logged = False
    server = sys.modules.get(SERVER)
    current = getattr(server, METHOD, None) if server is not None else None
    if getattr(current, _GUARD_FLAG, False):
        setattr(server, METHOD, current.__wrapped__)
