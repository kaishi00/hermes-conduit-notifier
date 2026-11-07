"""Hermes never parses this plugin's code to ask whether it is a memory provider.

Hermes lists the memory providers it can offer by looking at every plugin
folder (``plugins.memory._iter_provider_dirs``), and since 2026-10-09
("detect providers past the first 8 KB of __init__.py") a folder whose
``__init__.py`` doesn't name the contract in its first 8 KB has every
top-level module parsed with ``ast`` (``_defines_memory_provider``), on
every look, with nothing kept between looks. The dashboard looks each time
it builds its plugins hub (``_merged_plugins_hub``, kept for 5 s), and this
plugin is over a hundred modules, about 3.5 MB of Python: parsing them made
each build about 5 s of CPU on a server and minutes on a phone, enough to
stall the dashboard's event loop and drop its sockets (hermes-conduit#1931,
Hermes under Termux). This plugin is never a memory provider, so the plugin
answers that for its own folder and leaves every other folder to Hermes.

Each load of the plugin adds its folder to one set for the whole process,
kept outside this module, so a second copy (another profile's, a reload)
is answered too and one wrapper does it.
"""

from __future__ import annotations

import functools
import importlib
import logging
import sys
import threading
import types
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)
_lock = threading.Lock()

MODULE = "plugins.memory"
FUNCTION = "_defines_memory_provider"
_GUARD_FLAG = "__conduit_not_a_memory_provider__"
_SHARED = "conduit_push_memory_scan"
HERE = Path(__file__).resolve().parent

_wrapped_module: Any = None
_wrapped: Optional[tuple[Callable[..., Any], Callable[..., Any]]] = None


def _own_folders() -> set[str]:
    holder = sys.modules.setdefault(_SHARED, types.ModuleType(_SHARED))
    folders = getattr(holder, "folders", None)
    if not isinstance(folders, set):
        folders = holder.folders = set()
    return folders


def _is_own(path: Any) -> bool:
    try:
        return str(Path(path).resolve()) in _own_folders()
    except (OSError, TypeError, ValueError, RuntimeError):
        return False


def _guard(original: Callable[..., Any]) -> Callable[..., Any]:
    # Hands on whatever Hermes passes, so a change to the signature upstream
    # still reaches Hermes' own function.
    def defines_memory_provider(path: Any, *args: Any, **kwargs: Any) -> Any:
        if _is_own(path):
            return False
        return original(path, *args, **kwargs)

    functools.update_wrapper(defines_memory_provider, original)
    setattr(defines_memory_provider, _GUARD_FLAG, True)
    return defines_memory_provider


def install() -> bool:
    """Adds this plugin's folder to those Hermes doesn't parse, and wraps
    Hermes' parse once per process. False when this Hermes has no such
    parse (an older one only reads the first 8 KB, which is cheap)."""
    global _wrapped_module, _wrapped
    with _lock:
        _own_folders().add(str(HERE))
        if _wrapped_module is not None:
            return True
        try:
            memory = importlib.import_module(MODULE)
        except Exception:  # noqa: BLE001 - no Hermes memory plugins here (tests)
            logger.debug("conduit_push: no Hermes memory provider discovery to guard", exc_info=True)
            return False
        own = getattr(memory, FUNCTION, None)
        # A guard an earlier load of the plugin left behind wraps Hermes' own.
        while callable(own) and getattr(own, _GUARD_FLAG, False):
            own = getattr(own, "__wrapped__", None)
        if not callable(own):
            return False
        guard = _guard(own)
        setattr(memory, FUNCTION, guard)
        _wrapped_module, _wrapped = memory, (own, guard)
        return True


def uninstall() -> None:
    """Puts Hermes' own function back (the plugin unloads). A guard another
    load of the plugin has put on since is left to that load."""
    global _wrapped_module, _wrapped
    with _lock:
        module, wrapped = _wrapped_module, _wrapped
        _wrapped_module, _wrapped = None, None
        if module is not None and wrapped is not None and getattr(module, FUNCTION, None) is wrapped[1]:
            setattr(module, FUNCTION, wrapped[0])
