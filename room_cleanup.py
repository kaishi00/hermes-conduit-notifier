"""Disbanding a group room ends its agents' hidden chats on its home (designs/cross-owner-agents).

Each agent in a room Hermes hosts keeps a hidden session for it (source
bot_room, titled "Group: <room id>"), and Hermes keeps those after the room is
disbanded, though Conduit says disbanding ends them. Once Hermes' disband_room
returns, the plugin deletes the sessions of the home's own agents for that
room, in every room, the way Hermes' own delete does: with their compressed
history and their helpers' chats, but not a turn's that's still finishing. One
of those, or one Hermes' delete fails on, is tried again shortly, then a minute
after the plugin next loads, which also ends what rooms disbanded before this
version left behind. A room whose roster can't be read is looked for in every
agent's home on this host. Agents on other people's Hermes keep theirs (Eric,
2026-10-26); forget_room is how their owner deletes one later, from
`hermes conduit-push rooms forget` or Conduit.
"""

from __future__ import annotations

import functools
import importlib
import inspect
import json
import logging
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from . import room_rules

logger = logging.getLogger(__name__)

ROOMS = "gateway.hosted_rooms"
FUNCTION = "disband_room"
STATE_DB = "state.db"
SOURCE = "bot_room"
ROOM_TITLE = "Group: "
_GUARD_FLAG = "__conduit_room_guard__"
# Set on Hermes' module: one sweep for every copy of the plugin a process
# loads (the dashboard's and the plugin's own), until one unloads.
_SWEPT_FLAG = "__conduit_room_sweep__"
_ROOM_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
# Hermes numbers a title that's taken " #N" (hermes_state_titles).
_TITLE = re.compile(r"Group: ([A-Za-z0-9][A-Za-z0-9._:-]{0,127})(?: #\d+)?")
_PROFILE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
# What Hermes' delete raises for a session whose ledger still has a turn.
_BUSY = frozenset({"session_busy", "unknown_execution"})
# Hermes' delete, as the cleanup calls it (SessionDB.delete_sessions).
_DELETE_PARAMETERS = frozenset({"sessions_dir", "exclude_active_write_guards", "skipped_ids",
                                "include_compression_chain"})
# Hermes keeps at most 512 disbanded rooms in a store.
MAX_SWEEP_ROOMS = 512
MAX_ROOM_SESSIONS = 4096
ROOMS_PER_QUERY = 100
READ_TIMEOUT = 5.0
RETRY_AFTER_S = 30.0
SWEEP_AFTER_S = 60.0
# As Conduit's other routes word a store that can't be read (artifacts).
STORE_BUSY = "Hermes' session store is busy; try again shortly."

_lock = threading.Lock()
# Hermes' room module this load guards, Hermes' own function and this load's guard.
_guarded: Optional[tuple[Any, Callable[..., Any], Callable[..., Any]]] = None
_stop = threading.Event()
_sweeper: Optional[threading.Thread] = None
_logged: set[str] = set()


class CleanupError(RuntimeError):
    """Why a room's hidden chats weren't deleted; ``status`` is the HTTP
    status the route answers with."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def check_room_id(value: Any) -> str:
    if not isinstance(value, str) or not _ROOM_ID.fullmatch(value):
        raise CleanupError("That isn't a room id.")
    return value


def forget_room(room_id: Any, home: Path) -> dict[str, Any]:
    """Deletes this profile's hidden chats for a room, the way a disband
    deletes the home's: a member host's copy of a room hosted elsewhere, or
    what a disband left here. If the room is still going elsewhere, the
    agent starts over in a fresh session on its next turn there. A room
    still going here is the home's to disband."""
    room_id = check_room_id(room_id)
    if not _can_delete():
        raise CleanupError("This Hermes can't delete a chat with its earlier history; update Hermes.", 501)
    home = Path(home)
    if _going_here(room_id, home):
        raise CleanupError(f"Room {room_id} is hosted here and still going: disband it to end its chats.", 409)
    try:
        deleted, kept, failed = _delete(home, _room_sessions(home / STATE_DB, {room_id}))
    except Exception as error:  # noqa: BLE001 - sqlite's errors, or what Hermes raises opening its store
        logger.warning("conduit_push: couldn't delete room %s's hidden chats in %s", room_id, home, exc_info=True)
        raise CleanupError(STORE_BUSY, 503) from error
    if failed:
        raise CleanupError(f"Hermes couldn't delete {failed} hidden chat{'' if failed == 1 else 's'} for room "
                           f"{room_id}; try again shortly.", 503)
    return {"room_id": room_id, "deleted": deleted, "kept": kept}


def sweep(home: Optional[Path] = None) -> int:
    """Deletes what's left of the hidden chats of this host's agents in the
    rooms disbanded here (a turn that was still finishing, a room disbanded
    before this version), the newest rooms of each store first. Returns how
    many it deleted."""
    home = Path(home) if home is not None else _hermes_home()
    if home is None:
        return 0
    plan: dict[Path, set[str]] = {}
    for store in room_rules.room_stores(home):
        try:
            rooms = _disbanded(store)
        except Exception:  # noqa: BLE001 - another store may still be read
            logger.warning("conduit_push: couldn't read the disbanded rooms in %s; the plugin tries again when it "
                           "next loads", store, exc_info=True)
            continue
        homes = _homes({profile for _, profiles in rooms for profile in profiles}, store)
        for room_id, profiles in rooms:
            for profile in profiles:
                for member_home in homes.get(profile, ()):
                    plan.setdefault(member_home, set()).add(room_id)
    deleted, kept, failed = _delete_rooms(plan, "rooms disbanded on this host")
    if deleted:
        logger.info("conduit_push: deleted %d hidden chats of group rooms disbanded on this host", deleted)
    if kept:
        logger.info("conduit_push: %d hidden chats of group rooms disbanded on this host are still finishing a "
                    "turn; the plugin deletes them when it next loads", kept)
    if failed:
        logger.warning("conduit_push: couldn't delete every hidden chat of the group rooms disbanded on this host; "
                       "the plugin tries again when it next loads")
    return deleted


def _guard(original: Callable[..., Any]) -> Callable[..., Any]:
    def disband_room(db_path: Any, *args: Any, **kwargs: Any) -> Any:
        room_id = kwargs.get("room_id")
        # Read first: Hermes can prune the room's row in the disband's own transaction.
        profiles = _before(db_path, room_id)
        result = original(db_path, *args, **kwargs)
        if profiles is None:
            logger.warning("conduit_push: couldn't read room %s's members before it was disbanded; looking for its "
                           "hidden chats in every agent's home on this host", room_id)
        if profiles is None or profiles:
            try:
                _in_background(functools.partial(_end, room_id, profiles, Path(db_path)), "conduit-room-cleanup")
            except Exception:  # noqa: BLE001 - the room is disbanded either way
                logger.warning("conduit_push: couldn't start deleting the hidden chats of room %s; the plugin "
                               "deletes them when it next loads", room_id, exc_info=True)
        return result

    functools.update_wrapper(disband_room, original)
    setattr(disband_room, _GUARD_FLAG, True)
    return disband_room


def _before(db_path: Any, room_id: Any) -> Optional[tuple[str, ...]]:
    """The room's agents on this host; None when its roster can't be read."""
    if not isinstance(room_id, str) or not _ROOM_ID.fullmatch(room_id):
        return ()
    try:
        rows = _select(Path(db_path), "SELECT members_json FROM hosted_rooms WHERE room_id = ?", (room_id,))
        return _local_profiles(rows[0][0]) if rows else ()
    except Exception:  # noqa: BLE001 - the disband is Hermes' either way
        logger.debug("conduit_push: couldn't read room %s's members before it was disbanded", room_id, exc_info=True)
        return None


def _end(room_id: str, profiles: Optional[tuple[str, ...]], store: Path) -> None:
    """The cleanup after one disband, in every agent's home when the room's
    roster couldn't be read (None); never raises."""
    try:
        if profiles is None:
            profiles = _every_profile(store)
        plan = {home: {room_id} for homes in _homes(set(profiles), store).values() for home in homes}
        what = f"room {room_id}"
        _, kept, failed = _delete_rooms(plan, what)
        if (kept or failed) and not _stop.wait(RETRY_AFTER_S):
            _, kept, failed = _delete_rooms(plan, what)
        if kept:
            logger.info("conduit_push: %d of room %s's hidden chats on this host are still finishing a turn; the "
                        "plugin deletes them when it next loads", kept, room_id)
        if failed:
            logger.warning("conduit_push: couldn't delete every hidden chat of room %s on this host; the plugin "
                           "tries again when it next loads", room_id)
    except Exception:  # noqa: BLE001
        logger.warning("conduit_push: couldn't delete the hidden chats of room %s after it was disbanded; the plugin "
                       "tries again when it next loads", room_id, exc_info=True)


def _delete_rooms(plan: dict[Path, set[str]], what: str) -> tuple[int, int, int]:
    """(deleted, kept, failed) over each home's chats for its rooms; a home
    whose store fails is logged, counts as one failed, and the others still
    go."""
    deleted = kept = failed = 0
    for home, room_ids in plan.items():
        try:
            done, waiting, broken = _delete(home, _room_sessions(home / STATE_DB, room_ids))
        except Exception:  # noqa: BLE001
            logger.warning("conduit_push: couldn't delete the hidden chats of %s in %s", what, home, exc_info=True)
            failed += 1
            continue
        deleted += done
        kept += waiting
        failed += broken
    return deleted, kept, failed


def _delete(home: Path, session_ids: list[str]) -> tuple[int, int, int]:
    """(deleted, kept: a turn still finishing, failed) with Hermes' own
    delete, one chat at a time so one that's busy or fails keeps only
    itself."""
    if not session_ids:
        return 0, 0, 0
    registry = importlib.import_module("hermes_state_registry")
    db = registry.acquire(home / STATE_DB)
    deleted = kept = failed = 0
    try:
        for session_id in session_ids:
            skipped: list[str] = []
            try:
                deleted += db.delete_sessions(
                    [session_id], sessions_dir=home / "sessions", exclude_active_write_guards=True,
                    skipped_ids=skipped, include_compression_chain=True)
            except Exception as error:  # noqa: BLE001 - Hermes' RuntimeStoreError, or one its store raises
                if getattr(error, "reason", None) in _BUSY:
                    kept += 1
                else:
                    failed += 1
                    logger.warning("conduit_push: Hermes couldn't delete hidden chat %s in %s", session_id, home,
                                   exc_info=True)
                continue
            kept += len(skipped)
    finally:
        registry.release_or_close(db)
    return deleted, kept, failed


def _room_sessions(path: Path, room_ids: Iterable[str]) -> list[str]:
    """The hidden sessions in one store titled for these rooms, looked up by
    their titles a batch of rooms at a time. Opens it read-only, so a home
    without one gets none made."""
    if not path.is_file():
        return []
    rooms = sorted({room_id for room_id in room_ids if isinstance(room_id, str) and _ROOM_ID.fullmatch(room_id)})
    found: list[str] = []
    for start in range(0, len(rooms), ROOMS_PER_QUERY):
        batch = rooms[start:start + ROOMS_PER_QUERY]
        # The title, or the title Hermes numbered; a room id has no GLOB metacharacters.
        titles = " OR ".join("title = ? OR title GLOB ?" for _ in batch)
        params = [value for room_id in batch for value in (ROOM_TITLE + room_id, f"{ROOM_TITLE}{room_id} #[0-9]*")]
        rows = _select(path, f"SELECT id, title FROM sessions WHERE source = ? AND ({titles}) LIMIT ?",
                       (SOURCE, *params, MAX_ROOM_SESSIONS))
        wanted = set(batch)
        found += [str(row[0]) for row in rows
                  if isinstance(row[1], str) and (match := _TITLE.fullmatch(row[1])) and match.group(1) in wanted]
    return found


def _disbanded(store: Path) -> list[tuple[str, tuple[str, ...]]]:
    rows = _select(store, "SELECT room_id, members_json FROM hosted_rooms WHERE disbanded_at IS NOT NULL "
                          "ORDER BY disbanded_at DESC LIMIT ?", (MAX_SWEEP_ROOMS,))
    rooms = []
    everyone: Optional[tuple[str, ...]] = None
    for room_id, members in rows:
        if not isinstance(room_id, str) or not _ROOM_ID.fullmatch(room_id):
            continue
        try:
            profiles = _local_profiles(members)
        except (TypeError, ValueError, RecursionError):
            # A roster that can't be read: the room's chats are looked for in every agent's home.
            if everyone is None:
                everyone = _every_profile(store)
            profiles = everyone
        if profiles:
            rooms.append((room_id, profiles))
    return rooms


def _going_here(room_id: str, home: Path) -> bool:
    for store in room_rules.room_stores(home):
        try:
            rows = _select(store, "SELECT disbanded_at FROM hosted_rooms WHERE room_id = ?", (room_id,))
        except Exception as error:  # noqa: BLE001
            raise CleanupError(f"Couldn't tell whether room {room_id} is still going here; try again.", 503) from error
        if rows and rows[0][0] is None:
            return True
    return False


def _select(path: Path, sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
    """Rows of a read-only query; none from a store that isn't there or
    has no such table (no rooms, or another Hermes' shape of them)."""
    if not path.is_file():
        return []
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=READ_TIMEOUT)
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as error:
        if any(missing in str(error).lower() for missing in ("no such table", "no such column")):
            return []
        raise
    finally:
        conn.close()


def _local_profiles(members_json: Any) -> tuple[str, ...]:
    """The profiles of a roster's agents on this host (target local, or
    none). Raises ValueError for a roster that isn't one."""
    members = json.loads(members_json)
    if not isinstance(members, list):
        raise ValueError("the room's members aren't a list")
    profiles: list[str] = []
    for member in members:
        target = member.get("target") if isinstance(member, dict) else None
        if not isinstance(member, dict) or (target is not None and not (
                isinstance(target, dict) and target.get("kind", "local") == "local")):
            continue
        profile = member.get("profile")
        if isinstance(profile, str) and _PROFILE.fullmatch(profile) and profile not in profiles:
            profiles.append(profile)
    return tuple(profiles)


def _homes(profiles: set[str], store: Path) -> dict[str, list[Path]]:
    """Where each agent keeps its sessions: the home the store's profile
    seats it from (``hosted_rooms.profiles``), and its profile's home. Only
    homes with a session store."""
    configured = _configured_homes(store.parent) if profiles else {}
    homes: dict[str, list[Path]] = {}
    for profile in sorted(profiles):
        found = [configured[profile]] if profile in configured else []
        try:
            from hermes_cli.profiles import get_profile_dir

            found.append(Path(get_profile_dir(profile)))
        except Exception:  # noqa: BLE001 - a name Hermes won't place
            logger.debug("conduit_push: no home for profile %r", profile, exc_info=True)
        homes[profile] = [home for index, home in enumerate(found)
                          if home not in found[:index] and (home / STATE_DB).is_file()]
    return homes


def _every_profile(store: Path) -> tuple[str, ...]:
    """Every agent on this host, for a room whose roster can't be read:
    Hermes' profiles, and those ``hosted_rooms.profiles`` places."""
    names = set(_configured_homes(store.parent))
    try:
        from hermes_cli.profiles import list_profile_names

        names.update(list_profile_names())
    except Exception:  # noqa: BLE001 - the configured homes are still looked in
        logger.debug("conduit_push: couldn't list this host's profiles", exc_info=True)
    return tuple(sorted(name for name in names if isinstance(name, str) and _PROFILE.fullmatch(name)))


def _configured_homes(home: Path) -> dict[str, Path]:
    """The ``hosted_rooms.profiles`` homes in one profile's config
    (gateway/session_hosted_members.py): absolute paths only, as Hermes
    takes them."""
    try:
        from hermes_cli.config_effective import load_user_config_effective

        section = load_user_config_effective(home / "config.yaml").get("hosted_rooms")
    except Exception:  # noqa: BLE001 - profile homes still cover the room
        logger.debug("conduit_push: couldn't read hosted_rooms.profiles in %s", home, exc_info=True)
        return {}
    profiles = section.get("profiles") if isinstance(section, dict) else None
    if not isinstance(profiles, dict):
        return {}
    return {name: Path(value) for name, value in profiles.items()
            if isinstance(name, str) and isinstance(value, str) and Path(value).is_absolute()}


def _can_delete() -> bool:
    """Whether this Hermes deletes a session with its compression chain,
    skipping a turn's (SessionDB.delete_sessions), through its shared stores."""
    try:
        delete = importlib.import_module("hermes_state").SessionDB.delete_sessions
        registry = importlib.import_module("hermes_state_registry")
        parameters = set(inspect.signature(delete).parameters)
    except Exception:  # noqa: BLE001
        return False
    return (_DELETE_PARAMETERS <= parameters and callable(getattr(registry, "acquire", None))
            and callable(getattr(registry, "release_or_close", None)))


def _hermes_home() -> Optional[Path]:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:  # noqa: BLE001 - no Hermes runtime here
        return None


def _start(work: Callable[[], None], name: str) -> None:
    threading.Thread(target=work, name=name, daemon=True).start()


# Hermes' disband call returns before its room's chats are deleted.
_in_background = _start


def _sweep_later(stop: threading.Event) -> None:
    # Off the plugin's load, so Hermes starts as fast as before.
    if stop.wait(SWEEP_AFTER_S):
        return
    try:
        sweep()
    except Exception:  # noqa: BLE001
        logger.warning("conduit_push: couldn't delete the hidden chats of group rooms disbanded on this host",
                       exc_info=True)


def _missing(what: str) -> None:
    if what not in _logged:
        _logged.add(what)
        logger.info("conduit_push: %s, so disbanding a group room leaves its agents' hidden chats on this host", what)


def install() -> bool:
    """Guards Hermes' disband_room, once per process, and sweeps a minute
    after the first load since the plugin last unloaded (a load whose sweep
    can't start leaves it to the next). Imports it, since the plugin can
    load before the gateway or the dashboard starts its rooms; both room
    services look it up on the module when they disband. False when this
    Hermes has no rooms, or can't delete a chat whole."""
    global _guarded, _stop, _sweeper
    with _lock:
        if _guarded is not None:
            return True
        try:
            rooms = importlib.import_module(ROOMS)
        except Exception:  # noqa: BLE001 - no Hermes rooms here (tests, an older Hermes)
            logger.debug("conduit_push: no Hermes group rooms to clean up after", exc_info=True)
            return False
        own = getattr(rooms, FUNCTION, None)
        # A guard an earlier load of the plugin left behind wraps Hermes' own.
        while callable(own) and getattr(own, _GUARD_FLAG, False):
            own = getattr(own, "__wrapped__", None)
        if not callable(own):
            _missing(f"Hermes' group rooms have no {FUNCTION}")
            return False
        if not _can_delete():
            _missing("this Hermes can't delete a session with its compression chain")
            return False
        guard = _guard(own)
        setattr(rooms, FUNCTION, guard)
        _guarded = (rooms, own, guard)
        _stop = threading.Event()
        _sweeper = None
        if not getattr(rooms, _SWEPT_FLAG, False):
            try:
                sweeper = threading.Thread(target=_sweep_later, args=(_stop,), name="conduit-room-sweep",
                                           daemon=True)
                sweeper.start()
            except Exception:  # noqa: BLE001 - disbands are still guarded
                logger.warning("conduit_push: couldn't start deleting the hidden chats of group rooms disbanded on "
                               "this host; the plugin tries again when it next loads", exc_info=True)
            else:
                # Marked only once it runs, so the plugin's other copy (they
                # meet on Hermes' module) sweeps if this one couldn't. Both
                # sweeping is harmless: a chat is deleted once.
                setattr(rooms, _SWEPT_FLAG, True)
                _sweeper = sweeper
        return True


def uninstall() -> None:
    """Puts Hermes' own function back and stops a retry or a sweep that
    hasn't run (the plugin unloads), so the next load sweeps. A guard
    another load has set since is left to that load."""
    global _guarded
    with _lock:
        guarded, _guarded = _guarded, None
        _stop.set()
        if guarded is not None:
            rooms, own, guard = guarded
            if vars(rooms).get(FUNCTION) is guard:
                setattr(rooms, FUNCTION, own)
            setattr(rooms, _SWEPT_FLAG, False)
