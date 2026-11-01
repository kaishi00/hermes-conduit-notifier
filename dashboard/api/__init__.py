"""The dashboard API's modules, one per feature. plugin_api.py, the file
Hermes loads, loads this package and lists what each module holds."""

from __future__ import annotations

import importlib.util
import os
import sys
from typing import Dict

# In the order they run, which is also the order their routes are tried (the
# first match wins). Every module is created before the first one runs, so
# ``from . import watch_jobs`` at the top of a module only names it: what a
# module's top-level statements use while it loads (a base class, a default
# value, a constant made from another's) has to come from a module above it
# here, while its functions can use any of them.
MODULES = (
    "core",
    "gemini_qwen_live",
    "memory",
    "gpt_live",
    "grok_live",
    "speech_server",
    "local_live",
    "local_live_read_back",
    "local_live_agent",
    "voice_calls",
    "chats",
    "history",
    "pairing",
    "notifications",
    "hermes_calls",
    "settings",
    "linked_rooms",
    "link_asks",
    "pulse",
    "phone_data",
    "watch_tools",
    "watch_jobs",
    "watch_audio",
    "watch_local_live",
    "link_status",
    "desktop_views",
    "rooms",
    "workspace",
    "workstreams",
    "capabilities",
    "self_update",
)


def _load() -> Dict:
    modules = {}
    for name in MODULES:
        spec = importlib.util.spec_from_file_location(f"{__name__}.{name}", os.path.join(__path__[0], f"{name}.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        globals()[name] = module
        modules[name] = module
    for module in modules.values():
        module.__spec__.loader.exec_module(module)
    return modules


# The router Hermes mounts: core's, which every module adds its routes to.
router = _load()["core"].router
