"""Chats Hermes Desktop has selected right now, for the reply-ready push (#87).

A reply that comes back quickly in a chat a connected Hermes Desktop (or web
dashboard) window has selected sends no "reply ready" push: the user most
likely watched it arrive. A long turn still pushes (they have likely walked
away), and so does every other notification: other chats, background jobs,
approvals, questions, failed turns and calls. Nothing to set up: the
dashboard already follows each Desktop connection's selected chat (the
Desktop views of #454), it only can't see whether the window is in front.

The dashboard process knows the selection; the turn's hooks may run in
another process. So the dashboard publishes it here, per Hermes home, in
``conduit-desktop-selected.json``: the selected stored chat ids and when it
wrote them. It rewrites the file when the selection changes and renews it
while anything is selected; a file not renewed in time (the dashboard
stopped) counts as nothing selected. Anything that can't be read counts as
nothing selected too, so a fault here can only let a push through.

Standalone (no imports from the plugin package): the dashboard loads it from
its file, the plugin imports it.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

FILE_NAME = "conduit-desktop-selected.json"
# The dashboard renews a selection this often, and a reader trusts a file
# this long: a couple of missed renewals (the sweep runs every 5 s) still hold.
RENEW_S = 30.0
STALE_S = 45.0
# A reply that took longer than this pushes even in a selected chat.
QUICK_TURN_S = 120.0
MAX_IDS = 200


def publish(home: Any, stored_ids: Iterable[str], *, now: float | None = None) -> None:
    """Records the chats Desktop has selected in ``home`` (none removes the file)."""
    ids = sorted({value.strip() for value in stored_ids if isinstance(value, str) and value.strip()})[:MAX_IDS]
    path = Path(home) / FILE_NAME
    if not ids:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    now = time.time() if now is None else now
    # A fresh, private (0600) temp file of its own, never one already there.
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{FILE_NAME}.", suffix=".tmp")
    try:
        try:
            handle = os.fdopen(descriptor, "w", encoding="utf-8")
        except BaseException:
            os.close(descriptor)
            raise
        with handle:
            json.dump({"selected": ids, "at": now}, handle, separators=(",", ":"))
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def selected(home: Any, session_id: Any, *, now: float | None = None) -> bool:
    """True while a Desktop window has ``session_id`` (a stored chat id) selected."""
    if not isinstance(session_id, str) or not session_id.strip():
        return False
    try:
        raw = json.loads((Path(home) / FILE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return False
    if not isinstance(raw, dict):
        return False
    at, ids = raw.get("at"), raw.get("selected")
    if not isinstance(at, (int, float)) or isinstance(at, bool):
        return False
    try:
        if not math.isfinite(at):
            return False
    except OverflowError:  # an integer too large for a float
        return False
    # Only what publish writes: a longer list, or a time ahead of the clock
    # (an edited file, a clock step), counts as nothing selected.
    if not isinstance(ids, list) or len(ids) > MAX_IDS:
        return False
    now = time.time() if now is None else now
    return 0 <= now - at <= STALE_S and session_id.strip() in ids


def quick(started_at: Any, *, now: float | None = None) -> bool:
    """True for a turn that started at ``started_at`` (``time.monotonic()``)
    and ended within QUICK_TURN_S."""
    if not isinstance(started_at, (int, float)) or isinstance(started_at, bool):
        return False
    now = time.monotonic() if now is None else now
    return 0 <= now - started_at <= QUICK_TURN_S
