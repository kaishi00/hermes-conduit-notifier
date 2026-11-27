"""One slow summary no longer takes a chat's helper models away for ten minutes.

When a chat grows past its threshold, Hermes summarises it on the
compaction helper (auxiliary.compression, "auto" = the chat's model), all
within a deadline of its own. When a summary call runs into that deadline,
Hermes walks its fallback providers anyway, each of them fails at once
("timed out at the host compression deadline after 0s": the caller had
already stopped waiting), and Hermes puts each on hold for ten minutes as if
it had timed out itself (agent/auxiliary_client.py,
``_quarantine_fallback_candidate``; "request timed out" isn't one of the
short holds in agent/auxiliary_health.py). The chat's own provider is often
among them, so for ten minutes every compaction finds no helper, and Hermes
says "No auxiliary LLM provider configured ... Run `hermes setup`" though one
is (Furina's Bot Chat on Eric's server, 2026-11-27).

So a fallback that failed only because the deadline was already spent isn't
put on hold: it never had a chance to answer. Anything else is held as
Hermes holds it. And when compaction finds no helper because the ones it
has are on hold, the warning says so, and for how long, instead of asking
for a setup that is already done.
"""

from __future__ import annotations

import functools
import importlib
import logging
import threading
import time
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

CLIENT = "agent.auxiliary_client"
COMPRESSION = "agent.conversation_compression"
QUARANTINE = "_quarantine_fallback_candidate"
NOTICE = "_emit_feasibility_notice"
_GUARD_FLAG = "__conduit_helper_holds__"

TIMED_OUT = "request timed out"
# Hermes' own words for a call stopped because its caller's deadline passed
# (agent/auxiliary_client.py, the streamed and the Anthropic wire).
DEADLINE_SPENT = "host compression deadline"
# The start of Hermes' warning when "auto" found no helper
# (check_compression_model_feasibility); v0.19 and v0.23 both start so.
NO_PROVIDER = "⚠ No auxiliary LLM provider configured"

_lock = threading.Lock()
# (module, name, Hermes' own, the guard) for each wrapped function.
_installed: list[tuple[Any, str, Callable[..., Any], Callable[..., Any]]] = []


def _deadline_spent(client: Any, err: Any) -> bool:
    """Whether a fallback's failure was its caller's deadline, not the
    provider: Hermes' error says so, or the deadline had passed already."""
    if DEADLINE_SPENT in str(err or ""):
        return True
    current = getattr(client, "_current_aux_stream_deadline", None)
    try:
        deadline = current() if callable(current) else None
    except Exception:  # noqa: BLE001 - a Hermes without it
        return False
    return isinstance(deadline, (int, float)) and time.monotonic() >= deadline


def _quarantine_guard(client: Any, own: Callable[..., Any]) -> Callable[..., Any]:
    def quarantine(task: Any, fb_label: Any, fb_provider: Any, fb_err: Any, *args: Any, **kwargs: Any) -> Any:
        if kwargs.get("reason") == TIMED_OUT and _deadline_spent(client, fb_err):
            logger.info("conduit_push: Auxiliary %s: fallback %s didn't get to answer before the deadline (%s); "
                        "not putting it on hold", task or "call", fb_label, fb_err)
            return None
        return own(task, fb_label, fb_provider, fb_err, *args, **kwargs)

    functools.update_wrapper(quarantine, own)
    setattr(quarantine, _GUARD_FLAG, True)
    return quarantine


def _held(client: Any) -> tuple[list[str], float]:
    """This profile's helpers on hold now, by name, and the seconds until
    the first of them is back; ([], 0) when none is or it can't be told."""
    until = getattr(client, "_aux_unhealthy_until", None)
    if not isinstance(until, dict):
        return [], 0.0
    try:
        from hermes_constants import hermes_home_key
        home = hermes_home_key()
    except Exception:  # noqa: BLE001 - names every profile's then
        home = None
    now = time.time()
    names: list[str] = []
    soonest = 0.0
    for key, expires in list(until.items()):
        if not isinstance(expires, (int, float)) or expires <= now:
            continue
        parts = key if isinstance(key, tuple) else (key,)
        if home is not None and len(parts) > 1 and parts[0] != home:
            continue
        rest = parts[1:] if home is not None and len(parts) > 1 else parts
        if rest and rest[0] == "custom-endpoint" and len(rest) > 1:
            # Only the host: never a key in the URL's userinfo, path or query.
            name = urlsplit(str(rest[1]) if "://" in str(rest[1]) else f"//{rest[1]}").hostname or "a custom endpoint"
        else:
            name = str(rest[0]) if rest else ""
        if not name:
            continue
        if name not in names:
            names.append(name)
        soonest = expires - now if not soonest else min(soonest, expires - now)
    return names, soonest


def held_warning(names: list[str], seconds: float) -> str:
    """The warning for a compaction that found its helpers on hold; "" for none."""
    if not names:
        return ""
    minutes = max(1, round(seconds / 60))
    unit = f"minute{'s' if minutes != 1 else ''}"
    who = ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")
    if len(names) == 1:
        return (f"⚠ Hermes' helper model for summarising long chats ({who}) didn't answer in time and is paused "
                f"(about {minutes} more {unit}), so until then older messages will be cut without a summary.")
    return (f"⚠ Hermes' helper models for summarising long chats ({who}) didn't answer in time and are paused "
            f"(the first of them is back in about {minutes} {unit}), so until then older messages will be cut "
            "without a summary.")


def _notice_guard(client: Any, own: Callable[..., Any]) -> Callable[..., Any]:
    def emit(agent: Any, msg: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(msg, str) and msg.startswith(NO_PROVIDER):
            try:
                names, seconds = _held(client)
                if names:
                    msg = held_warning(names, seconds)
            except Exception:  # noqa: BLE001 - never break Hermes' check; its own words stand
                logger.debug("conduit_push: couldn't read the helpers on hold", exc_info=True)
        return own(agent, msg, *args, **kwargs)

    functools.update_wrapper(emit, own)
    setattr(emit, _GUARD_FLAG, True)
    return emit


def _wrap(module: Any, name: str, make: Callable[[Callable[..., Any]], Callable[..., Any]]) -> bool:
    own = getattr(module, name, None)
    # A guard an earlier load of the plugin left behind wraps Hermes' own.
    while callable(own) and getattr(own, _GUARD_FLAG, False):
        own = getattr(own, "__wrapped__", None)
    if not callable(own):
        logger.info("conduit_push: this Hermes has no %s.%s; left as Hermes has it", module.__name__, name)
        return False
    guard = make(own)
    setattr(module, name, guard)
    _installed.append((module, name, own, guard))
    return True


def install() -> bool:
    """Puts both guards on, once per process. False when this Hermes has
    neither (or isn't here: the relay, tests, the CLI)."""
    with _lock:
        if _installed:
            return True
        try:
            client = importlib.import_module(CLIENT)
        except Exception:  # noqa: BLE001 - not in Hermes' process
            logger.debug("conduit_push: no Hermes auxiliary client to guard", exc_info=True)
            return False
        held = _wrap(client, QUARANTINE, lambda own: _quarantine_guard(client, own))
        try:
            compression = importlib.import_module(COMPRESSION)
        except Exception:  # noqa: BLE001
            compression = None
        worded = compression is not None and _wrap(compression, NOTICE, lambda own: _notice_guard(client, own))
        return held or worded


def uninstall() -> None:
    """Puts Hermes' own back (the plugin unloads). A guard another load has
    set since is left to that load."""
    with _lock:
        while _installed:
            module, name, own, guard = _installed.pop()
            if getattr(module, name, None) is guard:
                setattr(module, name, own)
