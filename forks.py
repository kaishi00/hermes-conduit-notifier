"""A fork of a chat is never one of the chat's turns to the phone.

Hermes answers some questions from a fork of a live chat
(``agent.background_review.build_cache_parity_fork``): the self-improvement
review after a turn, ``/btw`` (Conduit's step summaries ask one), and
Conduit's predictions (dashboard/api/chats.py). The fork takes its chat's
session id, so the prompt cache stays warm, and a Hermes without its fix of
2026-09-10 ("isolate detached forks from lifecycle hooks"; Hermes 0.21 among
them) runs plugin hooks for the fork's turn as for the chat's own: its
answer went out as the chat's reply push (a prediction's raw JSON), its
start and end moved the chat's Lock Screen, and it took the notes kept for
the chat's next turn. The plugin wraps the fork builder so each fork's run
is marked, and the hooks take a marked run for a subagent's. A newer Hermes
runs no hooks for a fork, and the mark changes nothing there.
"""

from __future__ import annotations

import contextvars
import functools
import logging
import threading
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

_GUARD_FLAG = "__conduit_fork_marks__"
# Set for the length of a fork's run, on the thread that runs it: Hermes
# calls the turn's hooks there (a worker thread it starts on a copy of the
# context sees it too).
_in_fork: contextvars.ContextVar[bool] = contextvars.ContextVar("conduit_push_in_fork", default=False)

_lock = threading.Lock()
# The module this load wrapped, and (Hermes' own function, this load's wrapper)
_wrapped_module: Any = None
_wrapped: Optional[tuple[Callable[..., Any], Callable[..., Any]]] = None


def inside() -> bool:
    """Whether the code running now is a fork's run."""
    return _in_fork.get()


# The fork's whole life under the chat's id: its run, and its teardown in
# case a Hermes reports the end from there.
MARKED = ("run_conversation", "shutdown_memory_provider", "close")


def _marked(method: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(method)
    def run(*args: Any, **kwargs: Any) -> Any:
        token = _in_fork.set(True)
        try:
            return method(*args, **kwargs)
        finally:
            _in_fork.reset(token)

    return run


def _mark(fork: Any) -> None:
    for name in MARKED:
        method = getattr(fork, name, None)
        if not callable(method):
            continue
        try:
            setattr(fork, name, _marked(method))
        except Exception:  # noqa: BLE001 - the fork runs unmarked, as before
            logger.warning("conduit_push: couldn't mark a chat's fork; its answer may reach the phone", exc_info=True)
            return


def _build(original: Callable[..., Any]) -> Callable[..., Any]:
    # Hands on whatever Hermes passes and returns, so a change to the
    # signature upstream can't stop forks.
    def build_cache_parity_fork(*args: Any, **kwargs: Any) -> Any:
        built = original(*args, **kwargs)
        _mark(built[0] if isinstance(built, tuple) and built else built)
        return built

    functools.update_wrapper(build_cache_parity_fork, original)
    setattr(build_cache_parity_fork, _GUARD_FLAG, True)
    return build_cache_parity_fork


def install() -> bool:
    """Wraps Hermes' fork builder, once per process. False when this Hermes
    has none to wrap (its /btw and review then make no forks)."""
    global _wrapped_module, _wrapped
    with _lock:
        if _wrapped_module is not None:
            return True
        try:
            from agent import background_review
        except Exception:  # noqa: BLE001 - no Hermes agent here (tests)
            logger.debug("conduit_push: no Hermes fork builder to wrap", exc_info=True)
            return False
        own = getattr(background_review, "build_cache_parity_fork", None)
        # A wrapper an earlier load of the plugin left behind wraps Hermes' own.
        while callable(own) and getattr(own, _GUARD_FLAG, False):
            own = getattr(own, "__wrapped__", None)
        if not callable(own):
            logger.debug("conduit_push: this Hermes builds no chat forks")
            return False
        wrapper = _build(own)
        background_review.build_cache_parity_fork = wrapper
        _wrapped_module, _wrapped = background_review, (own, wrapper)
        return True


def uninstall() -> None:
    """Puts Hermes' own function back (the plugin unloads). A wrapper another
    load of the plugin has put on since is left to that load."""
    global _wrapped_module, _wrapped
    with _lock:
        module, wrapped = _wrapped_module, _wrapped
        _wrapped_module, _wrapped = None, None
        if module is not None and wrapped is not None and getattr(module, "build_cache_parity_fork", None) is wrapped[1]:
            module.build_cache_parity_fork = wrapped[0]
