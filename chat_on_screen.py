"""Chats Conduit has on screen while their reply runs, for the reply-ready
push: what desktop_selected.py (#87) does for Hermes Desktop.

A reply that lands in a chat the user is looking at in Conduit sends no
"reply ready" push: they watched it arrive. Conduit on the iPhone already
hides a banner while it's in front, but the push still goes to the paired
iPhone (and from there to the Apple Watch) when the user is chatting in
Conduit on a Mac or an iPad, or in the moments the phone locks around the
reply. Desktop's selected chat (desktop_selected.py) never covered Conduit:
its socket stays open for a while off screen, so the host can't tell from it
whether Conduit is in front. Conduit can, so it says so.

While a chat is open in Conduit, its scene active and a turn running there,
Conduit puts ``PUT /notifications/on-screen`` with the chat's ids and a hold:
the chat counts as on screen for that many seconds. Conduit renews the hold
while the turn runs (its interval stretched by Low Power and Low Data Mode
like its other polls, with the hold three renewals long) and lets the last
one run out after the turn ends, so the reply's own hooks still see it. It
sends a hold of 0 when the user leaves the chat or the app, and when nobody
has touched or typed in Conduit for 2 minutes of a long turn: a Mac or an
iPad that never locks is likely being left, and its reply should push, as
with Desktop's 2-minute rule. Nothing is sent while no turn runs, so an idle
chat left open costs no requests.

One small file per device kind (iPhone, iPad, Mac…) in
``conduit-on-screen/`` under the profile's home, each written whole through a
private temp file: two devices never write the same file, so no lock is
needed, and one leaving doesn't end another's hold. Two devices of the same
kind (two iPhones) share a file, so the later one's report replaces the
other's; that can only let a push through. The dashboard writes; the
turn's hooks (agent process) only read. A file that can't be read, has the
wrong shape or claims a hold longer than MAX_HOLD_S from now counts as
nothing on screen, so a fault here can only let a push through.

The hold is Conduit's word, so the host bounds it too: a turn that ran longer
than MAX_TURN_S pushes whatever the holds say (``attended``), so a Conduit
that keeps renewing by mistake can't quiet a long job.

Only the reply-ready push follows this. Approvals, questions, failures,
background tasks, cron runs and calls don't.

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

DIRECTORY = "conduit-on-screen"
# Conduit's TurnSurface places a chat can be on screen on.
DEVICES = ("iphone", "ipad", "mac", "vision", "watch")
MAX_HOLD_S = 600
# A turn longer than this pushes even while held (see attended).
MAX_TURN_S = 1800.0
# A chat's own ids: its runtime id, its stored id and the stored ids of the
# rows it continues.
MAX_IDS = 16
MAX_ID_LENGTH = 200


def parse(body: Any) -> tuple[str, list[str], float]:
    """(device, chat ids, hold seconds) from a request body; ValueError for
    one that isn't. A hold of 0 needs no ids."""
    if not isinstance(body, dict):
        raise ValueError("Expected a JSON object with device, session_ids and hold_s")
    device = body.get("device")
    if device not in DEVICES:
        raise ValueError(f"device must be one of {', '.join(DEVICES)}")
    hold = body.get("hold_s")
    # The range check alone also turns away nan, inf and an integer too large
    # for a float (which math.isfinite would raise on).
    if not isinstance(hold, (int, float)) or isinstance(hold, bool) or not 0 <= hold <= MAX_HOLD_S:
        raise ValueError(f"hold_s must be a number of seconds from 0 to {MAX_HOLD_S}")
    raw = body.get("session_ids", [])
    if not isinstance(raw, list) or len(raw) > MAX_IDS:
        raise ValueError(f"session_ids must be a list of at most {MAX_IDS} chat ids")
    if any(not isinstance(value, str) or not value.strip() or len(value.strip()) > MAX_ID_LENGTH for value in raw):
        raise ValueError("session_ids must be chat ids")
    ids = _ids(raw)
    if hold and not ids:
        raise ValueError("A hold needs the chat's session_ids")
    return device, ids, float(hold)


def hold(home: Any, device: str, session_ids: Iterable[str], hold_s: float, *, now: float | None = None) -> bool:
    """Records that ``device`` has the chat ``session_ids`` names on screen
    for ``hold_s`` more seconds, or no chat (0, which removes its file).
    True while something is held."""
    if device not in DEVICES:
        raise ValueError(f"device must be one of {', '.join(DEVICES)}")
    path = Path(home) / DIRECTORY / f"{device}.json"
    ids = _ids(session_ids)
    # Anything but a positive hold (0, a negative, nan) releases.
    if not ids or not isinstance(hold_s, (int, float)) or isinstance(hold_s, bool) or not hold_s > 0:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return False
    now = time.time() if now is None else now
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # A fresh, private (0600) temp file of its own, never one already there.
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{device}.", suffix=".tmp")
    try:
        try:
            handle = os.fdopen(descriptor, "w", encoding="utf-8")
        except BaseException:
            os.close(descriptor)
            raise
        with handle:
            json.dump({"ids": ids, "until": now + float(min(hold_s, MAX_HOLD_S))}, handle, separators=(",", ":"))
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return True


def attended(started_at: Any, *, now: float | None = None) -> bool:
    """True for a turn that started at ``started_at`` (``time.monotonic()``)
    no more than MAX_TURN_S ago; a turn with no noted start counts as not."""
    if not isinstance(started_at, (int, float)) or isinstance(started_at, bool):
        return False
    now = time.monotonic() if now is None else now
    return 0 <= now - started_at <= MAX_TURN_S


def on_screen(home: Any, session_id: Any, *, now: float | None = None) -> bool:
    """True while a Conduit device holds the chat ``session_id`` names on screen."""
    if not isinstance(session_id, str) or not session_id.strip():
        return False
    wanted = session_id.strip()
    now = time.time() if now is None else now
    directory = Path(home) / DIRECTORY
    for device in DEVICES:
        try:
            raw = json.loads((directory / f"{device}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            continue
        if _holds(raw, wanted, now):
            return True
    return False


def _holds(raw: Any, session_id: str, now: float) -> bool:
    if not isinstance(raw, dict):
        return False
    until, ids = raw.get("until"), raw.get("ids")
    if not isinstance(until, (int, float)) or isinstance(until, bool):
        return False
    try:
        if not math.isfinite(until):
            return False
    except OverflowError:  # an integer too large for a float
        return False
    # Only what hold writes: a longer list, or a hold further out than one
    # request can ask (an edited file, a clock step), counts as nothing.
    if not isinstance(ids, list) or len(ids) > MAX_IDS:
        return False
    return now < until <= now + MAX_HOLD_S and session_id in ids


def _ids(values: Iterable[Any]) -> list[str]:
    seen: list[str] = []
    for value in values:
        if isinstance(value, str):
            value = value.strip()
            if value and len(value) <= MAX_ID_LENGTH and value not in seen:
                seen.append(value)
    return seen[:MAX_IDS]
