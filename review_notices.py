"""Self-improvement updates follow the setting as it is now.

Conduit's "Self-improvement updates" setting is Hermes' own
``display.memory_notifications``. The dashboard's chats read it once, when
they build a chat's agent, and keep that agent while the gateway runs, so a
chat already open went on showing (or hiding) the "💾 Self-improvement
review" notices after the setting changed, until the gateway restarted.
Before each background review of a dashboard chat (Conduit's chats, Bot
Chat and group rooms all are), the plugin reads the setting again from that
chat's profile and puts it on the agent. Chats on messaging platforms read it
every turn already and are left alone.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

SETTING = "memory_notifications"
_GUARD_FLAG = "__conduit_review_notices__"

_lock = threading.Lock()
# The module this load wrapped, and (Hermes' own function, this load's wrapper)
_wrapped_module: Any = None
_wrapped: Optional[tuple[Callable[..., Any], Callable[..., Any]]] = None
_unread_count = 0


def _mode(display: Any) -> str:
    """The same reading Hermes gives the setting: a YAML bool is on or off,
    nothing set is on."""
    raw = display.get(SETTING) if isinstance(display, dict) else None
    if isinstance(raw, bool):
        return "on" if raw else "off"
    return str(raw).lower() if raw else "on"


def _session_home(agent: Any) -> tuple[bool, Optional[str]]:
    """Whether ``agent`` is a dashboard chat's, and the profile home that
    chat runs under (None for the gateway's own)."""
    server = sys.modules.get("tui_gateway.server")
    sessions = getattr(server, "_sessions", None)
    if not isinstance(sessions, dict):
        return False, None
    # Hermes' lock is reentrant, and the review runs on a thread of its own.
    with getattr(server, "_sessions_lock", None) or contextlib.nullcontext():
        records = list(sessions.values())
    for record in records:
        if isinstance(record, dict) and record.get("agent") is agent:
            home = record.get("profile_home") or getattr(server, "_hermes_home", None)
            return True, str(home) if home else None
    return False, None


def live_mode(agent: Any) -> Optional[str]:
    """The setting as the chat's profile has it now, or None when ``agent``
    isn't a dashboard chat's. Raises when the config can't be read."""
    found, home = _session_home(agent)
    if not found or not home:
        return None
    from hermes_cli.config_effective import load_user_config_effective

    return _mode(load_user_config_effective(Path(home) / "config.yaml").get("display"))


def _setting_unread() -> None:
    """Warns on the first failure and every 100th after: the chat then keeps
    the setting it opened with, and an operator should see that."""
    global _unread_count
    _unread_count += 1
    if _unread_count % 100 == 1:
        logger.warning("conduit_push: couldn't read the self-improvement updates setting (%d times so far); "
                       "open chats keep the one they started with", _unread_count, exc_info=True)
    else:
        logger.debug("conduit_push: couldn't read the self-improvement updates setting", exc_info=True)


def _run_review(original: Callable[..., Any]) -> Callable[..., Any]:
    # Hands on whatever Hermes passes, so a change to the signature upstream
    # can't stop reviews.
    def run_review_in_thread(agent: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            mode = live_mode(agent)
            if mode is not None:
                agent.memory_notifications = mode
        except Exception:  # noqa: BLE001 - the review runs with what the agent has
            _setting_unread()
        return original(agent, *args, **kwargs)

    functools.update_wrapper(run_review_in_thread, original)
    setattr(run_review_in_thread, _GUARD_FLAG, True)
    return run_review_in_thread


def install() -> bool:
    """Wraps Hermes' background review, once per process. False when this
    Hermes has none to wrap."""
    global _wrapped_module, _wrapped
    with _lock:
        if _wrapped_module is not None:
            return True
        try:
            from agent import background_review
        except Exception:  # noqa: BLE001 - no Hermes agent here (tests)
            logger.debug("conduit_push: no Hermes background review to wrap", exc_info=True)
            return False
        own = getattr(background_review, "_run_review_in_thread", None)
        # A wrapper an earlier load of the plugin left behind wraps Hermes' own.
        while callable(own) and getattr(own, _GUARD_FLAG, False):
            own = getattr(own, "__wrapped__", None)
        if not callable(own):
            logger.warning("conduit_push: Hermes' background review has no _run_review_in_thread, so chats "
                           "already open keep the self-improvement updates setting they started with")
            return False
        wrapper = _run_review(own)
        background_review._run_review_in_thread = wrapper
        _wrapped_module, _wrapped = background_review, (own, wrapper)
        return True


def uninstall() -> None:
    """Puts Hermes' own function back (the plugin unloads). A wrapper another
    load of the plugin has put on since is left to that load."""
    global _wrapped_module, _wrapped
    with _lock:
        module, wrapped = _wrapped_module, _wrapped
        _wrapped_module, _wrapped = None, None
        if module is not None and wrapped is not None and getattr(module, "_run_review_in_thread", None) is wrapped[1]:
            module._run_review_in_thread = wrapped[0]
