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
# Bot names by profile home; one entry per profile, so no cap is needed.
_names: dict[str, tuple[float, str]] = {}

# Conduit's Bot Mode finds each bot's one chat by this exact title, so every
# bot's chat carries it and can't be renamed. A push from one is titled with
# the bot's name instead, so it says which bot replied (#650).
BOT_CHAT_TITLE = "Bot Chat"
# Hermes caps a profile's display name at 64 characters.
BOT_NAME_MAX = 64
_cache_lock = threading.Lock()


def title(key: Any) -> str:
    """The session's title (by session id, else by gateway session key), or "".
    A Bot Chat's title is its bot's name instead."""
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
    if value == BOT_CHAT_TITLE:
        value = _bot_name(home)
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


def bot_name() -> str:
    """The name this profile's bot has in Hermes, as the Bots list shows it,
    or "" when it has none (the Bots list shows its profile id). Cached like
    titles, so a turn's several pushes read profile.yaml once."""
    home = _home()
    if home is None:
        return ""
    entry = str(home)
    now = time.monotonic()
    with _cache_lock:
        hit = _names.get(entry)
        if hit and now - hit[0] < CACHE_SECONDS:
            return hit[1]
    value = _given_name(home)
    with _cache_lock:
        _names[entry] = (time.monotonic(), value)
    return value


def _bot_name(home: Any) -> str:
    """The name the Bots list shows for the bot a Hermes home belongs to: the
    name it was given, else its profile id."""
    return _given_name(home) or _profile_of(home)


def _given_name(home: Any) -> str:
    """A bot's name as Desktop and Conduit's roster pick it: its Bot Mode
    title (``ui_meta['hermes-bots'].title`` in profile.yaml), else its
    profile's display name; "" when it has neither."""
    meta = _profile_meta(home)
    for value in (meta.get("bot_title"), meta.get("display_name")):
        name = " ".join(value.split()) if isinstance(value, str) else ""
        if name:
            return name[:BOT_NAME_MAX]
    return ""


def _profile_meta(home: Any) -> dict:
    """``bot_title`` and ``display_name`` from the profile's profile.yaml, as
    Hermes reads them; {} when it has none or it can't be read."""
    try:
        path = home / "profile.yaml"
        if not path.is_file():
            return {}
        data = _yaml_load(path.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001 - a title must never break the turn
        logger.debug("Couldn't read the profile of %s", home, exc_info=True)
        return {}
    if not isinstance(data, dict):
        return {}
    ui_meta = data.get("ui_meta")
    bots = ui_meta.get("hermes-bots") if isinstance(ui_meta, dict) else None
    return {
        "bot_title": bots.get("title") if isinstance(bots, dict) else None,
        "display_name": data.get("display_name"),
    }


def _yaml_load(text: str) -> Any:
    # Newer Hermes reads YAML through hermes_yaml (ruamel); older ones ship PyYAML.
    try:
        import hermes_yaml as yaml
    except ImportError:
        import yaml
    return yaml.safe_load(text)


def _profile_of(home: Any) -> str:
    """The profile a Hermes home belongs to: profiles/<name>, else the default."""
    try:
        name = str(home.name).strip()
        if home.parent.name == "profiles" and name:
            return name
    except Exception:  # noqa: BLE001 - a title must never break the turn
        logger.debug("Couldn't read the profile of %s", home, exc_info=True)
    return "default"


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
