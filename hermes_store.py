"""Hermes' stores (a profile's sessions, the group rooms' tables), through
whichever door this Hermes has.

Hermes' canary of 2026-11-18 removed ``SessionDB`` (notifier#883-#897): session
storage sits behind its storage interface (``persistence_assembly``), and
``hermes_state`` is a stand-in that carries only some of the old class's
methods, warns once, and goes in v0.27.0. A released Hermes (v0.23.2 and
before) has only ``SessionDB``.

``open_store(home)`` gives one object either way, with the ``SessionDB`` method
names the plugin calls: Hermes' own ``SessionDB`` on an older Hermes, and on a
newer one a ``_Store`` that runs each through the storage interface. Callers
keep calling ``get_meta``, ``get_session_title`` and the rest, and close it
(or use it in a ``with`` block) as before.

Group rooms moved the same way: Hermes' room calls (``gateway.hosted_rooms``,
``hosted_room_links``, ``hosted_room_replicas``) take a store handle and a
placement where they took the room database's path, and its room driver
holds ``store`` and ``placement`` rather than ``db_path``. The plugin still
names a room database by its file, which it also reads itself: ``room_call``
makes a room call either way, and ``room_db_path`` names the file a
``(store, placement)`` is. ``placement='profile'`` is the profile's own
``state.db``, ``'shared'`` the install's root ``shared-state.db``.

Loaded both as part of the plugin and by path from the dashboard routes
(dashboard/api/core.py's ``_hermes_store``), so it imports nothing of the
plugin's. Hermes' modules come through importlib, since each Hermes has only
one of the two doors (upstream/contract.yml watches both).
"""

from __future__ import annotations

import contextlib
import importlib
import inspect
import logging
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

logger = logging.getLogger(__name__)

STATE_DB = "state.db"
SHARED_STATE_DB = "shared-state.db"


def has_storage_interface() -> bool:
    """Whether this Hermes has the storage interface (its 2026-11-18 canary
    and later)."""
    try:
        assembly = importlib.import_module("persistence_assembly")
    except Exception:  # noqa: BLE001 - an older Hermes, or none here (tests)
        return False
    return callable(getattr(assembly, "open_persistence", None)) and callable(getattr(assembly, "profile_scope", None))


def _legacy_class() -> Any:
    return importlib.import_module("hermes_state").SessionDB


def open_store(home: Any, *, read_only: bool = False) -> Any:
    """The session store of the profile at ``home``, pinned to it (hooks and
    routes can run off the turn thread, where Hermes' ambient home lookup could
    fall back to the launch profile). Raises when there's none to open."""
    home = Path(home)
    if has_storage_interface():
        return _Store(home, read_only=read_only)
    db_class = _legacy_class()
    if read_only:
        return db_class(db_path=home / STATE_DB, read_only=True)
    return db_class(db_path=home / STATE_DB)


def can(method: str) -> bool:
    """Whether the store ``open_store`` gives on this Hermes has ``method``
    (an older Hermes may lack one), without opening it."""
    if has_storage_interface():
        return callable(getattr(_Store, method, None))
    try:
        return callable(getattr(_legacy_class(), method, None))
    except Exception:  # noqa: BLE001 - no Hermes here
        return False


def latest_row_of_key(db: Any, key: str) -> dict:
    """The latest session row of a gateway session key in an open store ({}
    when there's none), hidden rows included: a Bot Chat is one, and a key
    listing leaves them out unless asked (notifier#881). A Hermes from before
    that filter has no hidden rows to leave out."""
    try:
        rows = db.list_sessions_rich(session_key=key, limit=1, order_by_last_active=True, include_hidden=True)
    except TypeError:
        logger.debug("This Hermes lists no hidden rows by key; listing without them", exc_info=True)
        rows = db.list_sessions_rich(session_key=key, limit=1, order_by_last_active=True)
    return rows[0] if rows and isinstance(rows[0], dict) else {}


def new_session_id() -> Optional[str]:
    """A session id the way Hermes makes one; None when this Hermes can't say."""
    for module, name in (("session_rules.session_ids", "new_session_id"), ("hermes_state_ids", "new_session_id")):
        try:
            return getattr(importlib.import_module(module), name)()
        except (ImportError, AttributeError):
            continue
    return None


def _hermes(module: str) -> Any:
    return importlib.import_module(module)


def takes_store(call: Callable[..., Any]) -> bool:
    """Whether a Hermes room call takes a store handle and a placement
    (Hermes' 2026-11-18 canary on) rather than a database path."""
    try:
        first = next(iter(inspect.signature(call).parameters), None)
    except (TypeError, ValueError):
        return False
    return first == "store"


def room_db_path(store: Any, placement: Any = None) -> Path:
    """The room database file a Hermes room call's first argument names: a
    path as it is, or a store handle with its placement (Hermes'
    tui_gateway/hosted_room_service.py names it the same way)."""
    if isinstance(store, (str, Path)):
        return Path(store)
    profile_dir = Path(store.scope.profile_dir)
    if placement == "profile":
        return profile_dir / STATE_DB
    return Path(_hermes("gateway.hosted_rooms").default_db_path(profile_dir))


@contextlib.contextmanager
def room_store(db_path: Any) -> Iterator[tuple[Any, str]]:
    """A store handle on the room database at ``db_path`` and its placement,
    released on exit: a profile's ``state.db`` is ``'profile'``, the root
    ``shared-state.db`` ``'shared'``."""
    path = Path(db_path)
    assembly = _hermes("persistence_assembly")
    handle = assembly.open_persistence(assembly.profile_scope(home=path.parent))
    try:
        yield handle, "shared" if path.name == SHARED_STATE_DB else "profile"
    finally:
        handle.close()


def room_call(call: Callable[..., Any], db_path: Any, **fields: Any) -> Any:
    """Hermes' room call on the room database at ``db_path``, the way this
    Hermes takes it."""
    if not takes_store(call):
        return call(db_path, **fields)
    with room_store(db_path) as (store, placement):
        return call(store, placement=placement, **fields)


def service_call(call: Callable[..., Any], target: Any, **fields: Any) -> Any:
    """Hermes' room call on ``target``: a room service, on its own store
    handle and placement where both it and this Hermes take one, else on its
    database file; or a room database path, as ``room_call`` takes it."""
    if isinstance(target, (str, Path)):
        return room_call(call, target, **fields)
    store, placement = getattr(target, "store", None), getattr(target, "placement", None)
    if store is not None and placement is not None and takes_store(call):
        return call(store, placement=placement, **fields)
    return room_call(call, target.db_path, **fields)


def _media_root(home: Path) -> str:
    """The retained media root of the profile at ``home`` (``<documents
    cache>/native-inputs``, resolved), as Hermes' ``native_inputs_root`` names
    it for its own profile. A delete hands it to Hermes, whose hosted-document
    fence retires only the files under it, so it must be the store's own
    profile's: Hermes' resolver reads the ambient home, which off the turn
    thread or in a cleanup over every agent's home is another profile's, and
    that profile's files would go while its own leak (notifier#945)."""
    cache = _hermes("hermes_constants").get_hermes_dir("cache/documents", "document_cache", home=Path(home))
    return str((Path(cache) / "native-inputs").resolve())


class _Store:
    """The ``SessionDB`` methods the plugin calls, on Hermes' storage interface.
    The handle is opened (and the store created) here and released by
    ``close``."""

    def __init__(self, home: Path, *, read_only: bool = False) -> None:
        assembly = _hermes("persistence_assembly")
        self.home = home
        self.read_only = read_only
        # A reader is an owned handle behind a context manager; it never
        # creates, migrates or repairs the store.
        self._reader = assembly.open_persistence_reader(home) if read_only else None
        self._handle = self._reader.__enter__() if self._reader is not None \
            else assembly.open_persistence(assembly.profile_scope(home=home))

    def close(self) -> None:
        if self._reader is not None:
            self._reader.__exit__(None, None, None)
        else:
            self._handle.close()

    def __enter__(self) -> "_Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- sessions ------------------------------------------------------------

    def get_session(self, session_id: str) -> Optional[dict]:
        record = self._handle.sessions.get(session_id)
        return None if record is None else record.as_session_row()

    def get_session_title(self, session_id: str) -> Optional[str]:
        return self._handle.sessions.get_title(session_id)

    def get_session_title_source(self, session_id: str) -> Optional[str]:
        source = self._handle.sessions.get_title_source(session_id)
        return None if source is None else str(getattr(source, "value", source))

    def list_sessions_rich(self, *, session_key: Optional[str] = None, limit: Optional[int] = 20,
                           order_by_last_active: bool = False, include_hidden: bool = False) -> list:
        """The rows of a session key (the only listing the plugin asks for)."""
        sessions = _hermes("persistence.conversation.sessions")
        order = sessions.SessionOrder.LAST_ACTIVE if order_by_last_active else sessions.SessionOrder.STARTED_AT
        query = sessions.SessionQuery(session_key=session_key, order=order, include_hidden=include_hidden)
        return [entry.as_list_row()
                for entry in self._handle.sessions.list_sessions(query, sessions.SessionWindow(limit=limit))]

    def ensure_session(self, session_id: str, source: str = "unknown") -> str:
        spec = _hermes("persistence.conversation.sessions").NewSession(session_id=session_id, source=source)
        return self._handle.sessions.create(spec)

    def end_session(self, session_id: str, end_reason: str) -> None:
        self._handle.sessions.end(session_id, end_reason)

    def set_session_title(self, session_id: str, title: str) -> bool:
        # A held title raises TitleConflict, a ValueError, as the old class did.
        return self._handle.sessions.set_title(session_id, title)

    def set_session_hidden(self, session_id: str, hidden: bool) -> bool:
        return self._handle.sessions.set_hidden(session_id, hidden)

    def delete_session(self, session_id: str) -> bool:
        return bool(self._removed(self._handle.sessions.delete(session_id, media_root=self._media_root()),
                                  primary=session_id))

    def delete_sessions(self, session_ids: list, sessions_dir: Optional[Path] = None,
                        exclude_active_write_guards: bool = False, skipped_ids: Optional[list] = None,
                        include_compression_chain: bool = False) -> int:
        outcome = self._handle.sessions.delete_many(
            list(session_ids), skip_guarded=exclude_active_write_guards,
            include_compression_chain=include_compression_chain, media_root=self._media_root())
        self._removed(outcome.removed_ids, sessions_dir=sessions_dir)
        if skipped_ids is not None:
            skipped_ids.extend(outcome.skipped_guarded)
        return outcome.deleted

    def _media_root(self) -> str:
        return _media_root(self._handle.scope.profile_dir)

    def _removed(self, removed_ids: Any, *, primary: Optional[str] = None,
                 sessions_dir: Optional[Path] = None) -> Any:
        """What Hermes' own delete runs once its write commits: the plugins'
        hook, retired media, the removed chats' transcript files."""
        _hermes("session_retention").after_sessions_removed(
            self._handle, self._handle.scope.profile_dir, tuple(removed_ids), reason="delete", primary=primary,
            sessions_dir=sessions_dir if sessions_dir is not None else self.home / "sessions")
        return removed_ids

    def _auto_archive_lineage(self, session_id: str) -> bool:
        """Archives the chat with the idle sweep's stamp, so a turn there brings it back."""
        return bool(self._handle.maintenance.auto_archive_lineage(session_id))

    def unarchive_auto_archived(self, session_id: str) -> bool:
        """Un-archives a chat the idle sweep's stamp archived (the caller has
        checked the stamp), the way Hermes' reopen does."""
        return bool(self._handle.sessions.set_archived(session_id, False).changed)

    # -- messages ------------------------------------------------------------

    def get_messages(self, session_id: str) -> list:
        return [row.as_wire_row() for row in self._handle.messages.read_transcript(session_id)]

    def append_messages_batch(self, session_id: str, messages: list) -> int:
        if not messages:
            return 0
        new = _hermes("persistence.conversation.messages").NewMessage
        self._handle.messages.append_batch(session_id, [new.from_live(message) for message in messages])
        return len(messages)

    def append_completion_note(self, session_id: str, admission_id: str, content: str, display_kind: str,
                               metadata: dict) -> Any:
        return self._handle.messages.append_completion_note(session_id, admission_id, content, display_kind, metadata)

    def has_platform_message_id(self, session_id: str, platform_message_id: str) -> bool:
        return self._handle.messages.has_platform_message_id(session_id, platform_message_id)

    def take_unseen_reactions(self, session_id: str, author: str = "user") -> list:
        """The author's reactions not announced yet, stamped seen as they're
        read, as dicts the way the old class gave them (reactions)."""
        # Read field by field: they're stamped seen already, so one odd entry
        # mustn't lose the others.
        return [{"row_id": getattr(entry, "row_id", None), "role": getattr(entry, "role", ""),
                 "emoji": getattr(entry, "emoji", ""), "text": getattr(entry, "text", "")}
                for entry in self._handle.messages.take_unseen_reactions(session_id, author=author)]

    # -- state_meta ----------------------------------------------------------

    def get_meta(self, key: str) -> Optional[str]:
        return self._handle.state_meta.get(key)

    def set_meta(self, key: str, value: str) -> None:
        self._handle.state_meta.set(key, value)
