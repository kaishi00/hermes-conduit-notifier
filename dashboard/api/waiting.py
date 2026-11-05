"""Hermes is waiting on you: every chat of this profile with an approval or
a question waiting on the user, for Conduit's "Needs you" list, the counts
on its profile strip and its in-app banner (hermes-conduit issue 1705).

The waits are the ones the hooks keep in the profile's conduit-calls.json
(calls_store.note_waiting), for every chat now, whether or not alert calls
are on. The list only reads: an approval is answered on its chat's own card.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import HTTPException, Response

from . import core, hermes_calls, watch_local_live

router = core.router

# A wait Hermes gave up on (it denies an approval, or answers a question
# empty, after its timeout) leaves the list this long after, so a late
# answer still finds it there.
WAITING_GRACE_S = 30.0
# Hermes' clarify timeout when it can't be read.
CLARIFY_TIMEOUT_S = 3600.0
# Its own bucket: Conduit reads the list every 20 s per profile while it's
# on screen, which must never crowd out a call's presence or watch renewals
# in the calls routes' bucket.
WAITING_LIMIT = 120
WAITING_WINDOW_S = 60.0
_waiting_limiter = core._MintLimiter(WAITING_LIMIT, WAITING_WINDOW_S,
                                     message="Too many waiting-list reads; try again shortly")


def _clarify_timeout() -> Optional[float]:
    """Hermes' clarify timeout, None when it has none (0). Call in the
    profile's scope."""
    try:
        from tools.clarify_gateway import get_clarify_timeout

        value = float(get_clarify_timeout())
    except Exception:  # noqa: BLE001 — an older or different Hermes
        return CLARIFY_TIMEOUT_S
    return value if value > 0 else None


def _workstream_sessions(home: Any) -> list:
    """A bot's workstreams, which their Work panel shows; none when the
    registry can't be read."""
    try:
        return core._plugin_package_module("work_store").Store(Path(home)).session_ids()
    except Exception:  # noqa: BLE001 — an unreadable registry leaves nothing out
        return []


def waiting_list() -> Dict[str, Any]:
    home = core._calls_home()
    ages = {"approval": watch_local_live._hermes_approval_timeout() + WAITING_GRACE_S}
    clarify = _clarify_timeout()
    if clarify is not None:
        ages["question"] = clarify + WAITING_GRACE_S
    store = hermes_calls._calls_store().CallStore(home)
    return {"waiting": store.waiting_list(max_age=ages, leave_out=_workstream_sessions(home))}


@router.get("/waiting")
async def get_waiting(response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        return {"ok": True, **(await core._run_scoped(profile, core._limited(_waiting_limiter, profile, waiting_list)))}
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise core._unexpected("waiting list", exc, feature="Calls")
