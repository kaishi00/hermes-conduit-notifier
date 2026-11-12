"""The host's choices for what the plugin changes in Hermes' group rooms (notifier #443).

Two settings, in the root profile's config.yaml under
``plugins.entries.conduit_push.settings`` (where Hermes keeps a plugin's
settings, and where its Plugins page edits the ones plugin.yaml's
``config_schema`` declares; flat keys, since that page reads a dotted one as
a single key), read from the root because one host's rooms are hosted from
there:

- ``cleanup_disbanded_rooms``: ``delete`` (the default, Eric 2026-10-26) deletes this
  host's agents' hidden sessions for a room once it's disbanded
  (room_cleanup.py); ``off`` leaves them in Hermes, as Hermes alone would.
- ``conduit_rooms_only``: ``false`` (the default) lets the plugin's room
  changes reach every gateway-hosted room on the host; ``true`` keeps them to
  rooms Conduit made, for a Hermes others use rooms on too. Those changes are
  the ones to what a room's agents may do, remember or keep: the memory guard
  (room_memory.py), the owner rules for rooms with other people's agents
  (room_rules.py) and the disband cleanup (room_cleanup.py). The planner's
  fill-in (room_planner.py), the driver's wakeups (room_wakeups.py) and a
  cut-off member's single failed turn (room_cutoff.py) stay on for every room:
  they fix Hermes' driver getting stuck, and touch nothing of a room's.

Conduit names every room it makes ``conduit-<uuid>``, the app's create sheet
and Linked Rooms' drafts alike, and has since gateway-hosted rooms came in
(2026-09-26). A room whose id isn't known yet counts as Conduit's, so a room
the plugin can't tell apart keeps the plugin's changes.

The file is read again when it changes. Anything unreadable or misspelt is
the default, logged once. This module imports nothing from the plugin, so the
dashboard's room guards can load it by path.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

PLUGIN = "conduit_push"
CLEANUP_KEY = "cleanup_disbanded_rooms"
ONLY_KEY = "conduit_rooms_only"
DELETE, OFF = "delete", "off"
CLEANUP_CHOICES = (DELETE, OFF)
CONDUIT_ROOM_PREFIX = "conduit-"

_lock = threading.Lock()
# Root config path -> (mtime_ns, size, settings section)
_cache: dict[str, tuple[int, int, dict[str, Any]]] = {}
_logged: set[str] = set()


def cleanup_disbanded(home: Any = None) -> str:
    """``delete`` or ``off``."""
    value = _section(home).get(CLEANUP_KEY, DELETE)
    if isinstance(value, bool):
        # YAML reads a bare off as false.
        value = DELETE if value else OFF
    if isinstance(value, str) and value.strip().lower() in CLEANUP_CHOICES:
        return value.strip().lower()
    _once(f"cleanup {value!r}", "conduit_push: %s is %r, not delete or off; deleting a disbanded room's "
          "hidden chats as by default", CLEANUP_KEY, value)
    return DELETE


def conduit_rooms_only(home: Any = None) -> bool:
    value = _section(home).get(ONLY_KEY, False)
    if isinstance(value, bool):
        return value
    _once(f"only {value!r}", "conduit_push: %s is %r, not true or false; every room keeps the plugin's "
          "changes as by default", ONLY_KEY, value)
    return False


def is_conduit_room(room_id: Any) -> bool:
    return isinstance(room_id, str) and room_id.startswith(CONDUIT_ROOM_PREFIX)


def applies(room_id: Any, home: Any = None) -> bool:
    """Whether the plugin's room changes reach this room: every room, or
    with ``conduit_rooms_only`` Conduit's and those whose id isn't known
    (empty or None)."""
    if not room_id or is_conduit_room(room_id):
        return True
    return not conduit_rooms_only(home)


def root_home(home: Any = None) -> Optional[Path]:
    """The root Hermes home of ``home`` (this process's when None), as
    room_identity.root_home finds it; None without a Hermes runtime."""
    try:
        import hermes_constants

        if home is None:
            home = hermes_constants.get_hermes_home()
        root_of = getattr(hermes_constants, "get_default_hermes_root", None)
    except Exception:  # noqa: BLE001 - no Hermes runtime here (tests, a bare checkout)
        if home is None:
            return None
        root_of = None
    home = Path(home)
    if callable(root_of):
        try:
            return Path(root_of(home=home))
        except Exception:  # noqa: BLE001 - a Hermes that answers oddly gets the plain rule
            pass
    if home.parent.name == "profiles":
        return home.parent.parent
    return home


def _section(home: Any) -> dict[str, Any]:
    root = root_home(home)
    if root is None:
        return {}
    path = root / "config.yaml"
    try:
        stat = os.stat(path)
    except OSError:
        return {}
    key = str(path)
    with _lock:
        cached = _cache.get(key)
        if cached is not None and cached[:2] == (stat.st_mtime_ns, stat.st_size):
            return cached[2]
    try:
        section = _read(path)
    except Exception:  # noqa: BLE001 - a config Hermes itself would refuse: the defaults
        _once(f"read {key}", "conduit_push: couldn't read the room settings in %s; using the defaults", path,
              exc_info=True)
        section = {}
    with _lock:
        _cache[key] = (stat.st_mtime_ns, stat.st_size, section)
    return section


def _read(path: Path) -> dict[str, Any]:
    try:
        from hermes_cli.config_effective import load_user_config_effective
    except Exception:  # noqa: BLE001 - an older Hermes, or none: the file as written
        import yaml

        config = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    else:
        config = load_user_config_effective(path)
    section: Any = config
    for name in ("plugins", "entries", PLUGIN, "settings"):
        section = section.get(name) if isinstance(section, dict) else None
    return dict(section) if isinstance(section, dict) else {}


def _once(what: str, message: str, *args: Any, exc_info: bool = False) -> None:
    with _lock:
        if what in _logged:
            return
        _logged.add(what)
    logger.warning(message, *args, exc_info=exc_info)


def _clear() -> None:
    """Forgets what was read (tests)."""
    with _lock:
        _cache.clear()
        _logged.clear()
