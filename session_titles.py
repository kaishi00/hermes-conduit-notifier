"""Session titles for notifications, so a push says which chat it came from.

Shared by the lifecycle hooks and the clarify middleware. Never raises: without
a title the relay's generic copy stays in place.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

# A turn can raise several titled events (reply, approval, failure) in a few
# seconds; a short cache keeps those to one state-db read without hiding a rename
# for long. Only found titles are cached, so an untitled session picks up its
# title on the next event and a locked or missing store is retried. Entries are
# keyed by profile home: session ids and gateway keys don't name the profile.
CACHE_SECONDS = 30.0
MAX_CACHED = 256

_cache: dict[tuple[str, str], tuple[float, str]] = {}
_cache_lock = threading.Lock()


def title(key: Any) -> str:
    """The session's title (by session id, else by gateway session key), or ""."""
    key = key.strip() if isinstance(key, str) else ""
    if not key:
        return ""
    home = _home()
    if home is None:
        return ""
    entry = (str(home), key)
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(entry)
        if hit and now - hit[0] < CACHE_SECONDS:
            return hit[1]
        if hit:
            del _cache[entry]
    value = _read(home, key)
    if value:
        with _cache_lock:
            _cache.pop(entry, None)
            _cache[entry] = (time.monotonic(), value)
            while len(_cache) > MAX_CACHED:
                _cache.pop(next(iter(_cache)))
    return value


def titled(prefix: str, key: Any) -> str:
    value = title(key)
    return f"{prefix} · {value}" if value else prefix


def _home() -> Any:
    try:
        from hermes_constants import get_hermes_home

        return get_hermes_home()
    except Exception:  # noqa: BLE001 - no Hermes runtime here
        logger.debug("No Hermes home for notification titles", exc_info=True)
        return None


def _read(home: Any, key: str) -> str:
    try:
        from hermes_state import SessionDB

        # Pin the path: hooks can run off the turn thread, where the ambient home
        # lookup could fall back to the launch profile.
        db = SessionDB(db_path=home / "state.db")
    except Exception:  # noqa: BLE001 - no session store here (tests, older Hermes)
        logger.debug("No session store for notification titles", exc_info=True)
        return ""
    try:
        # Older Hermes may lack the id lookup; the key lookup can still work.
        getter = getattr(db, "get_session_title", None)
        value = getter(key) if getter else None
        if not value and hasattr(db, "list_sessions_rich"):
            # Approval hooks carry the gateway session key, not the session id.
            rows = db.list_sessions_rich(session_key=key, limit=1, order_by_last_active=True)
            value = rows[0].get("title") if rows else None
        return value.strip() if isinstance(value, str) else ""
    except Exception:  # noqa: BLE001 - a notification must never break the turn
        logger.debug("Couldn't read the session title for %s", key, exc_info=True)
        return ""
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            logger.debug("Couldn't close the session store", exc_info=True)
