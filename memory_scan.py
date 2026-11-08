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

Each load of the plugin counts its folder in one tally for the whole
process, kept outside this module, so a second copy (another profile's, a
reload, a symlinked profile copy that resolves to the same folder) is
answered too, one wrapper does it, and the wrapper comes off only when the
last loaded copy unloads.
"""

from __future__ import annotations

import functools
import importlib
import logging
import sys
import threading
import types
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)
_lock = threading.Lock()

MODULE = "plugins.memory"
FUNCTION = "_defines_memory_provider"
_GUARD_FLAG = "__conduit_not_a_memory_provider__"
_SHARED = "conduit_push_memory_scan"
HERE = Path(__file__).resolve().parent

# The module this load guarded, and whether this load counts in the tally.
_wrapped_module: Any = None
_counted = False


def _own_folders() -> dict[str, int]:
    """Each loaded copy's folder, with how many loaded copies resolve to it."""
    holder = sys.modules.setdefault(_SHARED, types.ModuleType(_SHARED))
    folders = getattr(holder, "tally", None)
    if not isinstance(folders, dict):
        folders = holder.tally = {}
    return folders


def _is_own(path: Any) -> bool:
    try:
        return _own_folders().get(str(Path(path).resolve()), 0) > 0
    except (OSError, TypeError, ValueError, RuntimeError):
        return False


def _guard(original: Callable[..., Any]) -> Callable[..., Any]:
    # Hands on whatever Hermes passes, so a change to the signature upstream
    # still reaches Hermes' own function; the folder is its first argument.
    def defines_memory_provider(*args: Any, **kwargs: Any) -> Any:
        if args and _is_own(args[0]):
            return False
        return original(*args, **kwargs)

    functools.update_wrapper(defines_memory_provider, original)
    setattr(defines_memory_provider, _GUARD_FLAG, True)
    return defines_memory_provider


def install() -> bool:
    """Adds this plugin's folder to those Hermes doesn't parse, and wraps
    Hermes' parse once per process. False when this Hermes has no such
    parse (an older one only reads the first 8 KB, which is cheap)."""
    global _wrapped_module, _counted
    with _lock:
        if not _counted:
            folders = _own_folders()
            folders[str(HERE)] = folders.get(str(HERE), 0) + 1
            _counted = True
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
            # Hermes before 0.21.7 has none and doesn't parse; a later one that
            # parses under another name would parse the plugin again.
            logger.info("conduit_push: Hermes' memory provider discovery has no %s to answer for the plugin; a "
                        "Hermes that parses plugins under another name parses this one on each listing", FUNCTION)
            return False
        guard = _guard(own)
        setattr(memory, FUNCTION, guard)
        _wrapped_module = memory
        return True


def uninstall() -> None:
    """Takes this plugin's folder off (the plugin unloads), and puts Hermes'
    own function back once no loaded copy's folder is left, whichever copy's
    guard is on."""
    global _wrapped_module, _counted
    with _lock:
        folders = _own_folders()
        if _counted:
            left = folders.get(str(HERE), 0) - 1
            if left > 0:
                folders[str(HERE)] = left
            else:
                folders.pop(str(HERE), None)
            _counted = False
        module = _wrapped_module
        _wrapped_module = None
        # Another copy still loaded keeps the guard, which answers for its folder.
        if folders or module is None:
            return
        # Whichever copy's guard is on, Hermes' own is under it.
        current = getattr(module, FUNCTION, None)
        own = current
        while callable(own) and getattr(own, _GUARD_FLAG, False):
            own = getattr(own, "__wrapped__", None)
        if callable(own) and own is not current:
            setattr(module, FUNCTION, own)
