"""A hidden turn still runs when Hermes compacts its chat.

Pulse's reports, briefs and follow-ups, and a bot's wake, are hidden turns
(``prompt.submit`` with ``display_kind: "hidden"``). When Hermes compacts the
chat at the start of a turn, or mid-turn, it checks that the next model call
has a real ask after the compaction's handoff (agent/conversation_loop.py,
``_should_skip_model_call_for_reference_handoff``, Hermes #80622). It counts
no row with a display kind as an ask, so a hidden turn's own ask doesn't count
either: Hermes skips the model call and writes its status line,
"Context was compacted. The previous response is complete — awaiting your
next message.", as the turn's reply. Pulse took that line as its report and
its morning brief (Eric's tester, 2026-11-06).

Until Hermes counts it, a hidden ask that is the chat's last row is this
turn's own, so the turn goes on to the model as an ask the user typed would.
Any other skip is left to Hermes. The status lines themselves are never a
reply worth a card or a push (``notice``).
"""

from __future__ import annotations

import functools
import importlib
import logging
import threading
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

LOOP = "agent.conversation_loop"
METHOD = "_should_skip_model_call_for_reference_handoff"
HIDDEN = "hidden"
_GUARD_FLAG = "__conduit_compaction_skip__"

# Hermes' final responses for a turn compaction ended without a model call
# (agent/conversation_loop.py, _HANDOFF_SKIP_FINAL_RESPONSE and
# _COMPRESSION_TIMEOUT_FINAL_RESPONSE), and what each means for Pulse's
# attempt. Read from Hermes when it's here, so a reworded line still matches.
SKIPPED = "Context was compacted. The previous response is complete — awaiting your next message."
TIMED_OUT = ("Context compression timed out without reducing this conversation. No messages were "
             "dropped. Start a fresh session with /new, or check auxiliary.compression before retrying /compress.")
SKIPPED_DETAIL = "Hermes compacted the chat and skipped the turn"
TIMED_OUT_DETAIL = "Hermes' compaction timed out"
_HERMES_NAMES = {"_HANDOFF_SKIP_FINAL_RESPONSE": SKIPPED_DETAIL,
                 "_COMPRESSION_TIMEOUT_FINAL_RESPONSE": TIMED_OUT_DETAIL}

_lock = threading.Lock()
# Hermes' loop module, its own check, and this load's guard.
_installed: Optional[tuple[Any, Callable[..., Any], Callable[..., Any]]] = None
_notices: Optional[dict[str, str]] = None


def _texts() -> dict[str, str]:
    """Each status line, as this Hermes words it when its loop can be read
    here, and as v0.23.0 did."""
    global _notices
    if _notices is None:
        texts = {" ".join(SKIPPED.split()): SKIPPED_DETAIL, " ".join(TIMED_OUT.split()): TIMED_OUT_DETAIL}
        try:
            loop = importlib.import_module(LOOP)
        except Exception:  # noqa: BLE001 - not in Hermes' process (the relay, tests, the CLI)
            loop = None
        for name, detail in _HERMES_NAMES.items():
            text = getattr(loop, name, None)
            if isinstance(text, str) and text.strip():
                texts[" ".join(text.split())] = detail
        _notices = texts
    return _notices


def notice(text: Any) -> str:
    """What Hermes' status line in place of a reply says happened (one of
    the ``*_DETAIL``), or "" for anything else, a real reply included."""
    if not isinstance(text, str) or len(text) > 400:
        return ""
    return _texts().get(" ".join(text.split()), "")


def _own_hidden_ask(messages: Any, user_message: Any) -> bool:
    """Whether the chat's last row is this turn's own ask, sent hidden."""
    last = messages[-1] if isinstance(messages, list) and messages else None
    return (isinstance(last, dict) and last.get("role") == "user" and last.get("display_kind") == HIDDEN
            and user_message is not None and last.get("content") == user_message)


def _guard(own: Callable[..., Any]) -> Callable[..., Any]:
    def check(messages: Any, user_message: Any, *args: Any, **kwargs: Any) -> Any:
        skip = own(messages, user_message, *args, **kwargs)
        if skip and _own_hidden_ask(messages, user_message):
            logger.info("conduit_push: Hermes compacted a chat during a hidden turn; the turn goes on to the "
                        "model instead of ending on Hermes' status line")
            return False
        return skip

    functools.update_wrapper(check, own)
    setattr(check, _GUARD_FLAG, True)
    return check


def install() -> bool:
    """Puts the guard on Hermes' check, once per process. False when this
    Hermes has no such check, whose hidden turns then go as Hermes has them."""
    global _installed
    with _lock:
        if _installed is not None:
            return True
        try:
            loop = importlib.import_module(LOOP)
        except Exception:  # noqa: BLE001 - not in Hermes' process (tests, the CLI)
            logger.debug("conduit_push: no Hermes conversation loop to guard", exc_info=True)
            return False
        own = getattr(loop, METHOD, None)
        # A guard an earlier load of the plugin left behind wraps Hermes' own.
        while callable(own) and getattr(own, _GUARD_FLAG, False):
            own = getattr(own, "__wrapped__", None)
        if not callable(own):
            logger.info("conduit_push: this Hermes has no %s, so a hidden turn compaction interrupts is "
                        "left as Hermes ends it", METHOD)
            return False
        guard = _guard(own)
        setattr(loop, METHOD, guard)
        _installed = (loop, own, guard)
        return True


def uninstall() -> None:
    """Puts Hermes' own check back (the plugin unloads). A guard another
    load has set since is left to that load."""
    global _installed
    with _lock:
        installed, _installed = _installed, None
        if installed is not None:
            loop, own, guard = installed
            if getattr(loop, METHOD, None) is guard:
                setattr(loop, METHOD, own)
