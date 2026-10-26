"""A member whose Hermes no longer accepts a room fails its turn once (designs/cross-owner-agents).

Hermes' room driver (tui_gateway/hosted_room_driver.py) puts a turn back in the
queue when a peer member's host refuses it before admission, and tries again
after a pause that grows to 30 s, forever. A revoked or expired grant is
refused that way too: Hermes records the member's route as needing
reauthorization (gateway/hosted_room_links), and the room waits on that member
from then on. Hermes marks the route ready again as it sets up each retry, so
the record holds only until then. Until Hermes settles such a turn itself, the
plugin reads the record before Hermes sets up the member's next queued turn,
and fails that turn before it's sent, with a reason, the way Hermes fails a
turn whose member has no destination, so the discussion goes on without
them. Linking the member again stores the route as ready, and its turns go
through again. A member that's only offline is Hermes' to retry.
"""

from __future__ import annotations

import functools
import importlib
import logging
import threading
from collections.abc import Mapping
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

DRIVER = "tui_gateway.hosted_room_driver"
RUNTIME = "HostedRoomRuntime"
METHOD = "_transport_for"
LINKS = "gateway.hosted_room_links"
CUT_OFF = "needs_reauthorization"
_GUARD_FLAG = "__conduit_room_guard__"

_lock = threading.Lock()
# The runtime class this load guards, Hermes' own method and this load's guard.
_guarded: Optional[tuple[Any, Callable[..., Any], Callable[..., Any]]] = None
_unreadable_logged = False


class MemberCutOff(RuntimeError):
    """The member's host no longer accepts the room: its grant was revoked
    or has run out."""


def _member_id(task: Mapping[str, Any]) -> str:
    # As Hermes' driver names a task's member (hosted_room_driver._member_id).
    payload = task.get("payload")
    payload = payload if isinstance(payload, Mapping) else {}
    return str(payload.get("target_member_id") or payload.get("target_profile") or "")


def _log_unreadable() -> None:
    global _unreadable_logged
    if _unreadable_logged:
        logger.debug("conduit_push: couldn't read a group room's stored routes", exc_info=True)
        return
    _unreadable_logged = True
    logger.warning("conduit_push: couldn't read a group room's stored routes, so a member whose Hermes no longer "
                   "accepts the room is retried as Hermes retries it (logged once)", exc_info=True)


def cut_off(db_path: Any, room_id: Any, member_id: str) -> bool:
    """Whether the room's stored route to this member says its host no
    longer accepts the room. False when the routes can't be read."""
    try:
        links, _malformed = importlib.import_module(LINKS).load_room_links_tolerant(db_path)
    except Exception:  # noqa: BLE001 - Hermes' behaviour stands
        _log_unreadable()
        return False
    for link in links:
        if getattr(link, "room_id", None) == room_id and getattr(link, "member_id", None) == member_id:
            return getattr(link, "status", None) == CUT_OFF
    return False


def _guard(original: Callable[..., Any]) -> Callable[..., Any]:
    # Takes whatever Hermes passes and hands it on as it came.
    def _transport_for(self: Any, binding: Any, task: Any, *args: Any, **kwargs: Any) -> Any:
        # Only a turn about to be sent: recovering or stopping one needs its
        # route. Read before Hermes builds the member's transport, which marks
        # the route ready again (its route status client counts the
        # transport's own bind_room_scope call as an answer from the member's
        # host). The home's own agents have no stored route.
        if isinstance(task, Mapping) and task.get("status") == "queued":
            member = _member_id(task)
            if member and cut_off(getattr(self, "db_path", None), getattr(binding, "room_id", None), member):
                raise MemberCutOff(f"cut off: {member}'s Hermes no longer accepts this room")
        return original(self, binding, task, *args, **kwargs)

    functools.update_wrapper(_transport_for, original)
    setattr(_transport_for, _GUARD_FLAG, True)
    return _transport_for


def install() -> bool:
    """Guards Hermes' room driver, once per process. Imports it, since the
    plugin can load before the gateway or the dashboard starts its rooms;
    every room service in the process runs that one runtime class. False
    when this Hermes has no such driver."""
    global _guarded
    with _lock:
        if _guarded is not None:
            return True
        try:
            runtime = getattr(importlib.import_module(DRIVER), RUNTIME)
        except Exception:  # noqa: BLE001 - no Hermes rooms here (tests, an older Hermes)
            logger.debug("conduit_push: no Hermes room driver to guard", exc_info=True)
            return False
        own = vars(runtime).get(METHOD)
        # A guard an earlier load of the plugin left behind wraps Hermes' own.
        while callable(own) and getattr(own, _GUARD_FLAG, False):
            own = getattr(own, "__wrapped__", None)
        if not callable(own):
            logger.info("conduit_push: Hermes' room driver has no %s, so a member whose Hermes no longer accepts "
                        "a room is retried as Hermes retries it", METHOD)
            return False
        guard = _guard(own)
        setattr(runtime, METHOD, guard)
        _guarded = (runtime, own, guard)
        return True


def uninstall() -> None:
    """Puts Hermes' own method back (the plugin unloads). A guard another
    load of the plugin has set since is left to that load."""
    global _guarded
    with _lock:
        guarded, _guarded = _guarded, None
        if guarded is not None:
            runtime, own, guard = guarded
            if vars(runtime).get(METHOD) is guard:
                setattr(runtime, METHOD, own)
