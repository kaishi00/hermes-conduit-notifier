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
runs no hooks for a fork, so there the mark only makes Conduit's tools
refuse a fork, as they refuse a subagent.

Both of Hermes' callers take the builder off the module when they fork
(``agent/side_question.py`` imports it inside ``_answer_via_fork``, and the
review calls its own module's global), and so does the predict route, so
each gets the wrapper. Neither a /btw nor a prediction can ask for an
approval or a question: both empty the thread's tool whitelist, so Hermes
denies every tool call before it runs. The review's approvals are denied by
Hermes (``_bg_review_auto_deny``).
"""

from __future__ import annotations

import contextvars
import functools
import logging
import sys
import threading
import types
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)
_lock = threading.Lock()

_GUARD_FLAG = "__conduit_fork_marks__"
# Set for the length of a fork's run, on the thread that runs it. Hermes
# calls the turn's hooks there (post_llm_call and on_session_end from the
# run's finalizer) and runs its tools on workers given a copy of the
# context (tool_executor's propagate_context_to_thread), which see it too.
# One for the whole process, kept outside this module: a second load of the
# plugin, or of this file, marks and reads the same one.
_SHARED = "conduit_push_fork_mark"


def _shared_mark() -> contextvars.ContextVar[bool]:
    holder = sys.modules.setdefault(_SHARED, types.ModuleType(_SHARED))
    mark = getattr(holder, "in_fork", None)
    if not isinstance(mark, contextvars.ContextVar):
        mark = holder.in_fork = contextvars.ContextVar("conduit_push_in_fork", default=False)
    return mark


_in_fork = _shared_mark()

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
    marked = False
    for name in MARKED:
        method = getattr(fork, name, None)
        if not callable(method):
            continue
        try:
            setattr(fork, name, _marked(method))
        except Exception:  # noqa: BLE001 - the fork runs unmarked, as before
            logger.warning("conduit_push: couldn't mark a chat's fork; its answer may reach the phone", exc_info=True)
            return
        marked = True
    if not marked:
        logger.warning("conduit_push: Hermes built a chat's fork the plugin doesn't know (%s), so it runs unmarked "
                       "and its answer may reach the phone", type(fork).__name__)


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
            # A Hermes that forks under another name would push forks again.
            logger.warning("conduit_push: Hermes' background review has no build_cache_parity_fork, so a fork of a "
                           "chat it builds otherwise may send pushes as the chat")
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
