"""Link status: the dashboard's Conduit card."""

from __future__ import annotations

import importlib.util
import os
import threading
import time
from typing import Any, Dict, Optional

from fastapi import HTTPException, Response

from . import core, desktop_views

logger = core.logger
router = core.router


# --- Link status ----------------------------------------------------------------
#
# The Conduit card on the dashboard's Sessions page (dist/index.js, slot
# sessions:top). Hermes' Connected Platforms card lists only the gateway's
# messaging adapters; Conduit is a dashboard client, so it says here instead
# whether the app reaches this host and whether its pushes get through.
# link_status.py keeps the times (it is shared with the hooks, which record
# each relay delivery); this side notes the app.

# The app counts as connected while one of its gateway sockets is open, or
# this soon after its last request.
LINK_APP_RECENT_S = 5 * 60.0
LINK_STATUS_PATH = "/link-status"

_link_status_module: Any = None
_link_status_lock = threading.Lock()
# Profile (as _limiter_key names it) -> when a request from Conduit last
# reached a route here.
_link_app_noted: Dict[str, float] = {}


def _link_status() -> Any:
    """link_status.py in the plugin's folder, loaded once."""
    global _link_status_module
    with _link_status_lock:
        if _link_status_module is None:
            path = os.path.join(core._plugin_root(), "link_status.py")
            spec = importlib.util.spec_from_file_location("conduit_push_dashboard_link_status", path)
            if spec is None or spec.loader is None:
                raise RuntimeError("This plugin install is missing link_status.py; update the plugin")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _link_status_module = module
        return _link_status_module


def _from_conduit(headers: Any) -> bool:
    """Conduit's requests: its sockets name themselves in X-Conduit-Client,
    and its URLSession requests carry no browser User-Agent. Only Conduit
    calls these routes (this plugin's dashboard bundle reads only the link
    status), so this just keeps a browser poking at them from counting."""
    if headers.get("x-conduit-client") is not None:
        return True
    return not str(headers.get("user-agent") or "").startswith("Mozilla/")


def _note_app_request(connection: Any) -> None:
    """Memory only (no I/O, no profile scope on every request): the link
    status route folds it in and saves it for the profile it reads."""
    try:
        if str(connection.url.path).endswith(LINK_STATUS_PATH) or not _from_conduit(connection.headers):
            return
        key = core._limiter_key(connection.query_params.get("profile"))
        now = time.time()
        with _link_status_lock:
            if key not in _link_app_noted and len(_link_app_noted) >= core.LIMITER_MAX_KEYS:
                # Bounded whatever profiles callers send: sightings past the
                # window go first, and a still-full map turns new keys away.
                for stale in [k for k, at in _link_app_noted.items() if now - at > LINK_APP_RECENT_S]:
                    del _link_app_noted[stale]
                if len(_link_app_noted) >= core.LIMITER_MAX_KEYS:
                    return
            _link_app_noted[key] = now
    except Exception:  # noqa: BLE001 — never fails Conduit's request
        logger.debug("Conduit: request not noted for the link status", exc_info=True)


def link_status_summary(home: Any, pairing: Optional[Dict[str, Any]], open_sockets: int,
                        observing: bool, *, requested_at: Optional[float] = None,
                        now: Optional[float] = None) -> Dict[str, Any]:
    """What the card shows for ``home``: the app's state and the push state.
    ``requested_at`` is this process's newest Conduit request, which the
    file may not have yet (its writes are throttled)."""
    now = time.time() if now is None else now
    recorded = _link_status().read(home)
    seen = [stamp for stamp in (recorded.get("app_seen_at"), requested_at) if stamp is not None]
    seen_at = max(seen) if seen else None
    if open_sockets > 0 or (seen_at is not None and 0 <= now - seen_at <= LINK_APP_RECENT_S):
        app_state = "connected"
    elif seen_at is not None:
        app_state = "idle"
    else:
        app_state = "never"
    ok_at, failed_at = recorded.get("push_ok_at"), recorded.get("push_failed_at")
    if pairing is None:
        push_state = "not_paired"
    elif pairing.get("relay_rejected_at"):
        push_state = "refused"
    elif pairing.get("relay_blocked_at"):
        push_state = "blocked"
    elif failed_at is not None and (ok_at is None or failed_at > ok_at):
        push_state = "failing"
    elif ok_at is not None:
        push_state = "ok"
    else:
        push_state = "waiting"
    return {
        "app": {"state": app_state, "seen_at": seen_at, "open_sockets": open_sockets,
                "sockets_observed": observing},
        "push": {"state": push_state, "ok_at": ok_at, "failed_at": failed_at,
                 "failed_reason": recorded.get("push_failed_reason") if push_state == "failing" else None,
                 "gateway_name": (pairing or {}).get("gateway_name") or None},
        "now": now,
    }


@router.get(LINK_STATUS_PATH)
async def get_link_status(response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    """The dashboard's Conduit card: whether the app reaches this profile, and
    whether its pushes get through the relay. No secrets: never the
    credential or the relay's address."""
    response.headers["Cache-Control"] = "no-store"
    desktop_views._desktop_view_hook.ensure_started()
    desktop_views._desktop_view_hook.try_install_quietly()

    def read() -> Dict[str, Any]:
        from hermes_constants import get_hermes_home

        home = get_hermes_home()
        with _link_status_lock:
            requested_at = _link_app_noted.get(core._limiter_key(profile))
        if requested_at is not None:
            _link_status().app_seen(home, now=requested_at)  # throttled there
        observing, _ = desktop_views._desktop_view_hook.status()
        return link_status_summary(home, core._load_pairing_state(core._pairing_state_path()),
                                   desktop_views._desktop_view_hook.conduit_sockets(home), observing,
                                   requested_at=requested_at)

    try:
        summary = await core._run_scoped(profile, read)
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise core._unexpected("read", exc, feature="Conduit status")
    return {"ok": True, **summary}
