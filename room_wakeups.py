"""A change in one group room doesn't keep every room's pass going.

Hermes' room driver (tui_gateway/hosted_room_driver.py) looks at each
gateway-hosted room on a thread of its own, up to four at once. A change, such
as a message or a reply, wakes the driver and marks every room whose pass is
still running, for one more pass once it ends. Hermes asks for that pass by
waking the driver the same way, which marks the passes still running then. So
with two rooms or more, their passes can keep marking each other, and the
driver looks at every room over and over, using CPU, until Hermes restarts.
Until Hermes changes it, a pass that ends only wakes the driver, which then
looks at its room again. Any other wake still marks every room being looked
at, as Hermes has it.
"""

from __future__ import annotations

import functools
import importlib
import inspect
import logging
import sys
import threading
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

DRIVER = "tui_gateway.hosted_room_driver"
RUNTIME = "HostedRoomRuntime"
METHOD = "wakeup"
# The room pass that wakes the driver as it ends.
PASS = "_run_room_once"
_GUARD_FLAG = "__conduit_room_wakeups__"

_lock = threading.Lock()
# The runtime class this load guards, Hermes' own wakeup and this load's guard.
_guarded: Optional[tuple[Any, Callable[..., Any], Callable[..., Any]]] = None


def _guard(own: Callable[..., Any], pass_code: Any) -> Callable[..., Any]:
    def wakeup(self: Any, *args: Any, **kwargs: Any) -> Any:
        # Called by Hermes' pass as it ends, for its own room's next pass. The
        # rooms still being looked at were marked by the change that marked
        # this one, if it touched them.
        wake = getattr(self, "_wake", None)
        if not args and not kwargs and wake is not None and sys._getframe(1).f_code is pass_code:
            wake.set()
            return None
        return own(self, *args, **kwargs)

    functools.update_wrapper(wakeup, own)
    setattr(wakeup, _GUARD_FLAG, True)
    return wakeup


def install() -> bool:
    """Guards Hermes' room driver, once per process. Imports it, since the
    plugin can load before the gateway or the dashboard starts its rooms;
    every room service in the process runs that one runtime class. False
    when this Hermes has no such driver, or one the guard can't read, whose
    rooms are then scheduled as Hermes schedules them."""
    global _guarded
    with _lock:
        if _guarded is not None:
            return True
        try:
            runtime = getattr(importlib.import_module(DRIVER), RUNTIME)
        except Exception:  # noqa: BLE001 - no Hermes rooms here (tests, an older Hermes)
            logger.debug("conduit_push: no Hermes room driver to guard", exc_info=True)
            return False
        try:
            own = vars(runtime).get(METHOD)
            # A guard an earlier load of the plugin left behind wraps Hermes' own.
            while callable(own) and getattr(own, _GUARD_FLAG, False):
                own = getattr(own, "__wrapped__", None)
            room_pass = vars(runtime).get(PASS)
            # The pass's own code, under anything that wraps it.
            pass_code = getattr(inspect.unwrap(room_pass), "__code__", None) if callable(room_pass) else None
            missing = (f"no {METHOD}" if not callable(own) else f"no {PASS}" if pass_code is None
                       # The pass asks for its room's next pass through wakeup.
                       else f"a {PASS} that doesn't call {METHOD}" if METHOD not in pass_code.co_names
                       else None)
            if missing is None:
                guard = _guard(own, pass_code)
                setattr(runtime, METHOD, guard)
        except Exception:  # noqa: BLE001 - a driver the guard can't read is left to Hermes
            logger.info("conduit_push: couldn't read Hermes' room driver, so its group rooms are scheduled as "
                        "Hermes schedules them", exc_info=True)
            return False
        if missing:
            logger.info("conduit_push: Hermes' room driver has %s, so its group rooms are scheduled as Hermes "
                        "schedules them", missing)
            return False
        _guarded = (runtime, own, guard)
        logger.debug("conduit_push: guarding the wakeups of Hermes' room driver from %s", pass_code.co_name)
        return True


def uninstall() -> None:
    """Puts Hermes' own wakeup back (the plugin unloads). A guard another
    load of the plugin has set since is left to that load."""
    global _guarded
    with _lock:
        guarded, _guarded = _guarded, None
        if guarded is not None:
            runtime, own, guard = guarded
            if vars(runtime).get(METHOD) is guard:
                setattr(runtime, METHOD, own)
