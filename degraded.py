"""What the plugin can't do on this Hermes right now, for Conduit to say.

The plugin leans on a few of Hermes' internals. When one has moved or can't
be read, the feature behind it carries on in a reduced way and says so in the
host's log, where nobody looks. Each such state is noted here under a short
code, and the capabilities route lists the codes (``degraded``), so Conduit's
plugin page says it too (notifier issue 834).

Hermes loads a copy of the plugin for each profile and the dashboard loads
its own, so the codes are kept on one object in ``sys.modules`` that every
copy in the process shares. They are this process's: a new one starts clean.
"""

from __future__ import annotations

import sys
import types

_HOLDER = "_conduit_push_degraded"

# A workstream a clean restart stops is left interrupted, not recovered:
# Hermes' turn stopper wasn't where work_tool wraps it.
WORKSTREAM_RECOVERY = "workstream-recovery"
# The Folder's routes refuse: Hermes has a managed-files policy and couldn't
# say which folder it keeps files in.
WORKSPACE_FILES = "workspace-files"


def _codes() -> dict:
    holder = sys.modules.get(_HOLDER)
    if holder is None:
        holder = sys.modules.setdefault(_HOLDER, types.ModuleType(_HOLDER))
    codes = getattr(holder, "codes", None)
    if codes is None:
        codes = holder.__dict__.setdefault("codes", {})
    return codes


def note(code: str) -> None:
    """``code`` holds until clear() or the process ends."""
    _codes()[code] = True


def clear(code: str) -> None:
    _codes().pop(code, None)


def current() -> list:
    """The codes that hold now, in a fixed order."""
    return sorted(_codes())
