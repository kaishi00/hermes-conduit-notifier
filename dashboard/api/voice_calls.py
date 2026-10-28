"""Voice transcripts and call end: a live call's turns saved into its chat."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import math
import os
import queue
import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from fastapi import HTTPException, Request, Response

from . import chats, core, gemini_qwen_live, memory

logger = core.logger
router = core.router


# --- Voice transcripts -------------------------------------------------------
#
# Conduit's live voice calls (Gemini Live, GPT-Live) never run a Hermes turn,
# so nothing records them. These routes write a call's settled turns straight
# into this profile's session store as an ordinary session row: no gateway,
# no agent, no model call. A resumed call appends to the same row.
#
# Rows keep source "desktop" and no model on purpose: Hermes treats a
# session's source as the agent platform (it picks the toolsets) and restores
# the stored model on resume, so typing into a saved call must look exactly
# like a Conduit chat. Conduit's own labels (voice call / classic voice chat /
# voice job) live in state_meta, which the agent never reads.
#
# A call attached to a chat (started in it, or a Hermes call from it) is part
# of that chat, not a session of its own (hermes-conduit#556): its turns are
# kept in state_meta under the call, listed for the chat, and the chat's own
# messages stay as Hermes wrote them. Its end writes the call to memory under
# the chat's session; the chat itself isn't ended.
VOICE_MAX_BODY_BYTES = 1024 * 1024
VOICE_MAX_TURNS = 500
VOICE_MAX_INDEX = 100_000
VOICE_MAX_TURN_CHARS = 16000
VOICE_MAX_TITLE_CHARS = 120
VOICE_MAX_SUMMARY_CHARS = 8000
VOICE_MAX_TAGS = 2000
VOICE_PRUNE_BATCH = 50
VOICE_TIMEOUT_S = 20.0
VOICE_WRITE_LIMIT = 120
VOICE_WRITE_WINDOW_S = 60.0
VOICE_READ_LIMIT = 600
VOICE_MAX_TIMESTAMP = 32_503_680_000  # year 3000
VOICE_SESSION_SOURCE = "desktop"
VOICE_END_REASON = "conduit_voice"
VOICE_TAG_KINDS = frozenset({"call", "classic", "job"})
# Local Live calls (the user's own speech server) save like the others.
VOICE_ENGINES = frozenset({"gemini-live", "gpt-live", "grok-live", "qwen-live", "local-live", "classic"})
VOICE_TAGS_KEY = "conduit.voice.tags"
VOICE_CALLS_KEY = "conduit.voice.calls:{session_id}"
VOICE_CREATED_KEY = "conduit.voice.created"
VOICE_MAX_CALLS = 500
VOICE_SUMMARY_KEY = "conduit.voice.summary:{session_id}"
VOICE_CHAT_CALL_KEY = "conduit.voice.chat_call:{call_id}"
VOICE_CHAT_CALLS_KEY = "conduit.voice.chat_calls:{chat_id}"
# A chat call is one meta value rewritten on each save: this bounds it.
VOICE_MAX_CHAT_CALL_TURNS = 2000
# Calls kept per chat; past it the oldest goes.
VOICE_MAX_CHAT_CALLS = 50
# Every chat's kept calls in one list, oldest first, so each device can file
# the chats under Voice and draw their call cards: call_id, chat_id, engine,
# started_at, ended_at. Written with each save; a host that predates it is
# read once from the calls themselves.
VOICE_CHAT_CALL_INDEX_KEY = "conduit.voice.chat_call_index"
VOICE_MAX_CHAT_CALL_INDEX = 300
_VOICE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_VOICE_UNSUPPORTED = "This Hermes version can't store voice transcripts; update Hermes"

_voice_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="conduit-voice")
# One writer at a time: the tag map is a read-modify-write JSON value, and a
# call's written-turn counter must not race a retried flush of the same call.
_voice_locks: Dict[str, threading.Lock] = {}
_voice_locks_guard = threading.Lock()


def _voice_lock() -> threading.Lock:
    """One writer at a time per profile, so a slow store in one profile never
    holds up another's (called inside the profile scope)."""
    from hermes_constants import get_hermes_home
    with _voice_locks_guard:
        return _voice_locks.setdefault(str(get_hermes_home()), threading.Lock())


# Conduit saves a call once, when it ends (retried from its outbox if that
# fails), plus a tag or summary; this only stops a runaway client from growing
# state.db without bound.
_voice_write_limiter = core._MintLimiter(VOICE_WRITE_LIMIT, VOICE_WRITE_WINDOW_S,
                                    message="Too many voice history writes; try again shortly")
_voice_read_limiter = core._MintLimiter(VOICE_READ_LIMIT, VOICE_WRITE_WINDOW_S,
                                   message="Too many voice history reads; try again shortly")
_VOICE_READ = ("get_meta",)
_VOICE_WRITE = ("get_meta", "set_meta", "get_session")
_VOICE_SAVE = _VOICE_WRITE + ("ensure_session",)


def _voice_id(value: Any, what: str) -> str:
    text = str(value or "").strip()
    if not _VOICE_ID_RE.match(text):
        raise core.TokenError(400, f"{what} is missing or invalid")
    return text


def _open_voice_db(needs=_VOICE_READ, appends: bool = False):
    """This profile's session store (profile-scoped by _run_scoped); 501 without the methods *needs*,
    or, with *appends*, without either message writer."""
    try:
        from hermes_constants import get_hermes_home
        from hermes_state import SessionDB
    except ImportError as exc:
        if exc.name not in ("hermes_state", "hermes_constants"):
            raise
        raise core.TokenError(501, _VOICE_UNSUPPORTED)
    # Pin the path: this runs on a worker thread, where Hermes' ambient home
    # lookup could fall back to the launch profile.
    db = SessionDB(db_path=get_hermes_home() / "state.db")
    can_append = hasattr(db, "append_messages_batch") or hasattr(db, "append_message")
    if (appends and not can_append) or not all(hasattr(db, name) for name in needs):
        db.close()
        raise core.TokenError(501, _VOICE_UNSUPPORTED)
    return db


def _voice_tags(db) -> Dict[str, Dict[str, Any]]:
    try:
        tags = json.loads(db.get_meta(VOICE_TAGS_KEY) or "{}")
    except ValueError:
        logger.warning("Conduit voice tags were unreadable; starting over")
        return {}
    return {k: v for k, v in tags.items() if isinstance(v, dict)} if isinstance(tags, dict) else {}


def _set_voice_tag(db, session_id: str, tag: Dict[str, Any]) -> None:
    # The session isn't required to exist yet: Conduit may tag a chat it just
    # created before Hermes has persisted it. Dangling tags go at the cap.
    tags = _voice_tags(db)
    tags.pop(session_id, None)
    tags[session_id] = tag  # re-inserted last, so it's the newest
    if len(tags) > VOICE_MAX_TAGS:
        # Drop tags whose sessions were deleted, then the oldest if still over;
        # the tag being written always stays.
        # Checks only the oldest few, so a request's work stays bounded.
        oldest = [sid for sid in tags if sid != session_id][:VOICE_PRUNE_BATCH]
        gone = [sid for sid in oldest if not db.get_session(sid)]
        for sid in gone:
            del tags[sid]
            _drop_voice_meta(db, sid)  # a row deleted elsewhere leaves no meta behind
        while len(tags) > VOICE_MAX_TAGS:
            tags.pop(next(iter(tags)))
    db.set_meta(VOICE_TAGS_KEY, json.dumps(tags, separators=(",", ":")))


def _voice_turns(raw: Any) -> list:
    if not isinstance(raw, list):
        raise core.TokenError(400, "turns must be a list")
    if len(raw) > VOICE_MAX_TURNS:
        raise core.TokenError(413, f"At most {VOICE_MAX_TURNS} turns per request")
    turns = []
    for item in raw:
        if not isinstance(item, dict):
            raise core.TokenError(400, "Each turn must be an object")
        role = item.get("role")
        text = item.get("text")
        index = item.get("index")
        if role not in ("user", "assistant") or not isinstance(index, int) or isinstance(index, bool) \
                or not 0 <= index <= VOICE_MAX_INDEX:
            raise core.TokenError(400, "Each turn needs a role (user or assistant) and an index from 0 to "
                                  f"{VOICE_MAX_INDEX}")
        if text is not None and not isinstance(text, str):
            raise core.TokenError(400, "A turn's text must be a string")
        # A turn is final once sent: an empty one could only be a turn that
        # isn't done yet, and its index must never change content later.
        text = (text or "").strip()
        if not text:
            raise core.TokenError(400, "Each turn needs text; send a turn only once it's final")
        at = item.get("at")
        turn = {
            "index": index,
            "role": role,
            "content": text[:VOICE_MAX_TURN_CHARS],
            "timestamp": at if isinstance(at, (int, float)) and not isinstance(at, bool)
            and math.isfinite(at) and 0 < at < VOICE_MAX_TIMESTAMP else None,
        }
        # Cards the live model showed (show_on_screen): kept with the turn so
        # the call's transcript draws them again. Only valid ones.
        if role == "assistant" and item.get("cards") is not None:
            schema = _card_schema()
            cards = schema.normalize_cards(item.get("cards")) if schema else []
            if cards:
                turn["cards"] = cards
        turns.append(turn)
    turns.sort(key=lambda turn: turn["index"])
    return turns


_card_schema_module: Any = None
_card_schema_lock = threading.Lock()
_CARD_SCHEMA_MISSING = object()


def _card_schema() -> Any:
    """card_schema.py in the plugin's folder, loaded once. None in an install
    that lost it or its schema: calls are then saved without their cards."""
    global _card_schema_module
    with _card_schema_lock:
        if _card_schema_module is None:
            path = os.path.join(core._plugin_root(), "card_schema.py")
            spec = importlib.util.spec_from_file_location("conduit_push_dashboard_card_schema", path)
            try:
                if spec is None or spec.loader is None or not os.path.exists(path):
                    raise ImportError("card_schema.py is missing")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            except Exception:  # noqa: BLE001 — a broken install draws no cards
                module = _CARD_SCHEMA_MISSING
            _card_schema_module = module
        return None if _card_schema_module is _CARD_SCHEMA_MISSING else _card_schema_module


def _voice_turn_receipt(call_id: str, index: int) -> str:
    """The receipt a stored turn carries as its platform_message_id: the call
    and the turn's index, so a retried save recognizes exactly the turns an
    earlier attempt already stored (the same dedupe Hermes' gateway runs on a
    retried user turn, via SessionDB.has_platform_message_id)."""
    return f"conduit-voice:{call_id}:{index}"


def _row_message(turn: Dict[str, Any], call_id: str) -> Dict[str, Any]:
    """A turn as a row's message: a session row holds text only, so a turn's
    cards become their Markdown after its text. The receipt rides along so a
    retried save that lands after its append but before the watermark write
    drops the turns the row already holds instead of writing them twice."""
    message = {k: v for k, v in turn.items() if k not in ("index", "cards") and v is not None}
    message["platform_message_id"] = _voice_turn_receipt(call_id, turn["index"])
    schema = _card_schema() if turn.get("cards") else None
    if schema:
        markdown = schema.fallback_markdown(turn["cards"])
        message["content"] = f"{message['content']}\n\n{markdown}"[:VOICE_MAX_TURN_CHARS]
    return message


def _new_voice_session_id() -> str:
    try:
        from hermes_state_ids import new_session_id
        return new_session_id()
    except ImportError:
        import uuid
        return f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:12]}"


def _set_voice_title(db, session_id: str, title: str) -> None:
    # Titles are unique per profile; a clash gets the id's tail, and a second
    # failure leaves the row untitled rather than failing the save.
    for candidate in (title, f"{title} ({session_id[-6:]})"):
        try:
            db.set_session_title(session_id, candidate)
            return
        except ValueError:  # the title is taken
            continue
        except Exception:
            # A title is never worth the transcript: keep the row untitled.
            logger.warning("Couldn't title Conduit voice session %s", session_id, exc_info=True)
            return
    logger.info("Conduit voice session %s kept no title", session_id)


def _voice_text(value: Any, limit: int) -> str:
    # Only strings: an object or list must not be stored as its repr.
    return gemini_qwen_live._clip(value, limit) if isinstance(value, str) else ""


def _append_voice_messages(db, session_id: str, messages: list) -> None:
    if hasattr(db, "append_messages_batch"):
        db.append_messages_batch(session_id, messages)
    else:  # older Hermes: one row at a time
        for message in messages:
            db.append_message(session_id, message["role"], content=message["content"],
                              timestamp=message.get("timestamp"))


def _voice_calls(db, session_id: str) -> Dict[str, int]:
    """The highest turn index written per call_id for this session."""
    try:
        calls = json.loads(db.get_meta(VOICE_CALLS_KEY.format(session_id=session_id)) or "{}")
    except ValueError:
        return {}
    if not isinstance(calls, dict):
        return {}
    return {k: v for k, v in calls.items() if isinstance(v, int) and not isinstance(v, bool)}


def _record_voice_call(db, session_id: str, calls: Dict[str, int], call_id: str, last: int) -> None:
    # One small int per call, in one row per session. Past VOICE_MAX_CALLS
    # calls the oldest are dropped; a call that old is never retried.
    calls.pop(call_id, None)
    calls[call_id] = last
    while len(calls) > VOICE_MAX_CALLS:
        calls.pop(next(iter(calls)))
    db.set_meta(VOICE_CALLS_KEY.format(session_id=session_id), json.dumps(calls, separators=(",", ":")))


def _created_voice_rows(db) -> Dict[str, str]:
    try:
        created = json.loads(db.get_meta(VOICE_CREATED_KEY) or "{}")
    except ValueError:
        return {}
    return {k: v for k, v in created.items() if isinstance(v, str)} if isinstance(created, dict) else {}


def _forget_created_voice_row(db, session_id: str) -> None:
    created = _created_voice_rows(db)
    kept = {call: sid for call, sid in created.items() if sid != session_id}
    if len(kept) != len(created):
        db.set_meta(VOICE_CREATED_KEY, json.dumps(kept, separators=(",", ":")))


def _remember_created_voice_row(db, call_id: str, session_id: str) -> None:
    # Which row each call created, so a create whose response was lost and is
    # retried lands in that row instead of a second one. Only recent calls
    # can be retried, so the map keeps the last VOICE_MAX_CALLS.
    created = _created_voice_rows(db)
    created.pop(call_id, None)
    created[call_id] = session_id
    while len(created) > VOICE_MAX_CALLS:
        created.pop(next(iter(created)))
    db.set_meta(VOICE_CREATED_KEY, json.dumps(created, separators=(",", ":")))


def _is_voice_row(db, session_id: str, row: Any) -> bool:
    """Appends go only to a row this plugin saved, never to an ordinary chat."""
    if isinstance(row, dict) and row.get("end_reason") == VOICE_END_REASON:
        return True
    return _voice_tags(db).get(session_id, {}).get("kind") == "call"


def _drop_voice_meta(db, session_id: str) -> None:
    """Clears a gone row's call record, summary and create record (Hermes has no meta delete)."""
    for key in (VOICE_CALLS_KEY, VOICE_SUMMARY_KEY):
        key = key.format(session_id=session_id)
        if db.get_meta(key):
            db.set_meta(key, "")
    _forget_created_voice_row(db, session_id)


def _discard_new_voice_session(db, session_id: str) -> None:
    """Best effort: don't leave an empty or half-written row behind a failed create."""
    try:
        if hasattr(db, "delete_session"):
            db.delete_session(session_id)
        _drop_voice_meta(db, session_id)
        tags = _voice_tags(db)
        if tags.pop(session_id, None) is not None:
            db.set_meta(VOICE_TAGS_KEY, json.dumps(tags, separators=(",", ":")))
    except Exception:
        logger.warning("Couldn't discard the failed Conduit voice session %s", session_id, exc_info=True)


def save_voice_turns(body: Dict[str, Any],
                     on_end: Optional[Callable[..., None]] = None) -> Dict[str, Any]:
    """Create a voice-call session or append to one; turns already written for the call are skipped.

    ``on_end(session_id, engine, messages, chat_call=False)`` runs once new turns are stored:
    Conduit saves a call only after it has ended, so those turns end it. It runs
    with the store closed but the save lock held, so a session's ends keep the
    order its turns were stored in; it must be quick and must not raise (the
    turns are already stored). A retry that finds its turns already stored by
    receipt (an earlier attempt died before its watermark, so before its end)
    still ends the call, with those turns.

    ``skipped`` counts every turn this save didn't append: replays below the
    watermark and turns the row already held by receipt.
    """
    call_id = _voice_id(body.get("call_id"), "call_id")
    engine = str(body.get("engine") or "").strip()
    if engine not in VOICE_ENGINES:
        raise core.TokenError(400, "engine is missing or invalid")
    turns = _voice_turns(body.get("turns"))
    title = _voice_text(body.get("title"), VOICE_MAX_TITLE_CHARS)
    requested = body.get("session_id")
    session_id = _voice_id(requested, "session_id") if requested else None
    # A call that already has a row keeps appending to it; chat_id only
    # decides where a call without one goes. A chat call has no title (one is
    # only set when a row is created).
    if body.get("chat_id") is not None and not session_id:
        return _save_chat_call_turns(_voice_id(body.get("chat_id"), "chat_id"), call_id, engine, turns, on_end)
    with _voice_lock():
        db = _open_voice_db(_VOICE_SAVE, appends=True)
        try:
            created = adopted = False
            if session_id:
                row = db.get_session(session_id)
                if not row:
                    _drop_voice_meta(db, session_id)
                    raise core.TokenError(422, "That voice session no longer exists; start a new one")
                if not _is_voice_row(db, session_id, row):
                    raise core.TokenError(422, "That session isn't a saved voice call; start a new one")
            else:
                earlier = _created_voice_rows(db).get(call_id)
                if earlier and db.get_session(earlier):
                    session_id = earlier  # a retried create: continue that row
                    adopted = True
                elif earlier:
                    _drop_voice_meta(db, earlier)
            if not session_id:
                if not turns:
                    return {"session_id": None, "written": 0, "appended": 0, "skipped": 0, "created": False}
                if sorted({turn["index"] for turn in turns}) != list(range(len({turn["index"] for turn in turns}))):
                    raise core.TokenError(400, "Turns must continue from index 0 for this call")
                session_id = _new_voice_session_id()
                db.ensure_session(session_id, source=VOICE_SESSION_SOURCE)
                created = True
            try:
                if created:
                    _remember_created_voice_row(db, call_id, session_id)
                calls = _voice_calls(db, session_id)
                last = calls.get(call_id, -1)
                # A call's indices are contiguous from 0 and a row is
                # append-only, so every index up to `last` is already stored:
                # those are replays of a retried save and are skipped. New
                # turns must continue at last + 1; a gap is refused rather
                # than stored out of order or silently lost.
                fresh = []
                for turn in turns:  # sorted by index
                    if turn["index"] <= last:
                        continue
                    if turn["index"] != last + 1:
                        raise core.TokenError(400, f"Turns must continue from index {last + 1} for this call")
                    fresh.append(turn)
                    last = turn["index"]
                # The append and the watermark are two separate commits, so a
                # save can store its turns and die before the watermark write;
                # the outbox then retries the same turns. A turn the row
                # already holds is recognized by its receipt and dropped here
                # instead of written twice, while the watermark below still
                # advances past it. Hosts without the receipt lookup (older
                # Hermes) keep the old best-effort behavior, and so do hosts
                # whose only writer is append_message: it doesn't store the
                # receipt, so the lookup never finds one.
                to_end = fresh
                receipts_dropped = 0
                if fresh and hasattr(db, "has_platform_message_id"):
                    kept = []
                    for turn in fresh:
                        if db.has_platform_message_id(session_id, _voice_turn_receipt(call_id, turn["index"])):
                            receipts_dropped += 1
                            continue
                        kept.append(turn)
                    if receipts_dropped:
                        fresh = kept
                skipped = len(turns) - len(fresh)
                if skipped:
                    logger.debug("Conduit voice save for %s skipped %d already-written turn(s)", session_id, skipped)
                messages = [_row_message(turn, call_id) for turn in fresh]
                if messages:
                    _append_voice_messages(db, session_id, messages)
                # The watermark is written before the end runs, so turns
                # dropped by receipt were stored by an attempt that died before
                # its end: this save ends the call, with all of them.
                end_messages = [_row_message(turn, call_id) for turn in to_end]
                if fresh or receipts_dropped:
                    _record_voice_call(db, session_id, calls, call_id, last)
                if hasattr(db, "end_session") and (created or adopted or fresh or receipts_dropped):
                    # A saved call is never a live chat; the first end wins, so
                    # later flushes are no-ops. Hermes refuses appends only to rows
                    # that compression closed, so this end doesn't block them.
                    db.end_session(session_id, VOICE_END_REASON)
                if created and title and hasattr(db, "set_session_title"):
                    _set_voice_title(db, session_id, title)
                # The tag records a successful archive, so a retried create
                # finishes a first attempt that died before tagging or whose
                # archive failed, and leaves alone a row the user un-archived.
                tag = {} if created else _voice_tags(db).get(session_id, {}) if adopted else None
                if tag is not None and (tag.get("kind") != "call" or not tag.get("archived")):
                    archived = _archive_ended_voice_row(db, session_id)
                    _set_voice_tag(db, session_id, {"kind": "call", "engine": engine,
                                                    **({"archived": True} if archived else {})})
            except Exception:
                if created:
                    _discard_new_voice_session(db, session_id)
                raise
            # `written` is one past the highest index the host has for this
            # call: where the next save starts.
            result = {"session_id": session_id, "written": last + 1, "appended": len(messages),
                      "skipped": skipped, "created": created}
        finally:
            db.close()
        if end_messages and on_end is not None:
            on_end(session_id, engine, end_messages)
    return result


def _archive_ended_voice_row(db, session_id: str) -> bool:
    """Keeps a standalone call out of Desktop's chat list (hermes-conduit#1124).

    Archived with Hermes' idle-sweep stamp (``auto_archived``), not as a user
    archive: the first turn anyone types into the row reopens it, and Hermes
    then un-archives it by itself, so a call continued as a chat comes back.
    Conduit still lists these rows with its calls. Only a call's first save
    (or a retry finishing it) archives its row; a resumed call leaves the
    flag as the user left it.
    Hosts without the stamp keep the row visible. Never fails the save.
    True when the row was archived."""
    archive = getattr(db, "_auto_archive_lineage", None)
    if archive is None:
        return False
    try:
        archive(session_id)
    except Exception:  # noqa: BLE001 — the turns are stored; the row just stays listed
        logger.warning("Couldn't archive Conduit voice call %s", session_id, exc_info=True)
        return False
    return True


def _chat_call(db, call_id: str) -> Optional[Dict[str, Any]]:
    try:
        stored = json.loads(db.get_meta(VOICE_CHAT_CALL_KEY.format(call_id=call_id)) or "null")
    except ValueError:
        logger.warning("Conduit voice: chat call %s is unreadable", call_id)
        return None
    if not isinstance(stored, dict) or not isinstance(stored.get("chat_id"), str) \
            or not isinstance(stored.get("turns"), list):
        return None
    return stored


def _chat_call_ids(db, chat_id: str) -> list:
    try:
        ids = json.loads(db.get_meta(VOICE_CHAT_CALLS_KEY.format(chat_id=chat_id)) or "[]")
    except ValueError:
        logger.warning("Conduit voice: chat %s's call list is unreadable", chat_id)
        return []
    return [i for i in ids if isinstance(i, str) and _VOICE_ID_RE.match(i)] if isinstance(ids, list) else []


def _chat_call_index_entry(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    call_id, chat_id = raw.get("call_id"), raw.get("chat_id")
    if not isinstance(call_id, str) or not isinstance(chat_id, str) \
            or not _VOICE_ID_RE.match(call_id) or not _VOICE_ID_RE.match(chat_id):
        return None
    entry: Dict[str, Any] = {"call_id": call_id, "chat_id": chat_id}
    if isinstance(raw.get("engine"), str):
        entry["engine"] = raw["engine"]
    for key in ("started_at", "ended_at"):
        value = raw.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value < VOICE_MAX_TIMESTAMP:
            entry[key] = value
    return entry


def _stored_chat_call_index(db) -> Optional[list]:
    """The list as stored; None when this host has never written it, or
    when it's unreadable (it's read from the calls again then)."""
    raw = db.get_meta(VOICE_CHAT_CALL_INDEX_KEY)
    if raw is None:
        return None
    try:
        stored = json.loads(raw or "[]")
    except ValueError:
        logger.warning("Conduit voice: the chat call list is unreadable; reading it from the calls")
        return None
    entries = [_chat_call_index_entry(e) for e in stored] if isinstance(stored, list) else []
    return [e for e in entries if e is not None]


def _write_chat_call_index(db, entries: list) -> None:
    db.set_meta(VOICE_CHAT_CALL_INDEX_KEY, json.dumps(entries[-VOICE_MAX_CHAT_CALL_INDEX:], separators=(",", ":")))


def _turn_time(turn: Any) -> Optional[float]:
    value = turn.get("timestamp") if isinstance(turn, dict) else None
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _index_chat_call(db, call_id: str, chat_id: str, engine: Optional[str], turns: list) -> None:
    """Lists the call with its first and last turn's times: a new one as the
    newest, a listed one where it was, so a later save doesn't reorder it."""
    entries = _stored_chat_call_index(db)
    if entries is None:
        entries = _chat_calls_on_disk()
        if entries is None:
            return  # unread, so left unwritten: the next read lists this call with the rest
    times = [t for t in (_turn_time(turn) for turn in turns) if t is not None]
    entry = _chat_call_index_entry({"call_id": call_id, "chat_id": chat_id, "engine": engine,
                                    "started_at": times[0] if times else None,
                                    "ended_at": times[-1] if times else None})
    at = next((i for i, e in enumerate(entries) if e["call_id"] == call_id), None)
    if at is not None:
        del entries[at]
    if entry is not None:
        entries.insert(len(entries) if at is None else at, entry)
    _write_chat_call_index(db, entries)


def _unindex_chat_calls(db, gone: Callable[[Dict[str, Any]], bool]) -> None:
    entries = _stored_chat_call_index(db)
    if entries and any(gone(e) for e in entries):
        _write_chat_call_index(db, [e for e in entries if not gone(e)])


def _chat_calls_on_disk() -> Optional[list]:
    """The calls a host kept before the list existed, read once from the
    calls themselves (oldest first), without loading their turns. None when
    they couldn't be read."""
    from hermes_constants import get_hermes_home
    path = get_hermes_home() / "state.db"
    if not path.exists():
        return []
    prefix = VOICE_CHAT_CALL_KEY.format(call_id="")
    pattern = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=1.0)
        try:
            rows = conn.execute(
                "SELECT substr(key, ?), json_extract(value, '$.chat_id'), json_extract(value, '$.engine'), "
                "json_extract(value, '$.turns[0].timestamp'), "
                # Not '#-1': that needs SQLite 3.31.
                "json_extract(value, '$.turns[' || (json_array_length(value, '$.turns') - 1) || '].timestamp') "
                "FROM state_meta WHERE key LIKE ? ESCAPE '\\' AND json_valid(value)",
                (len(prefix) + 1, pattern)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        logger.warning("Conduit voice: couldn't read the calls kept with chats", exc_info=True)
        return None
    entries = [_chat_call_index_entry({"call_id": r[0], "chat_id": r[1], "engine": r[2],
                                       "started_at": r[3], "ended_at": r[4]}) for r in rows]
    entries = [e for e in entries if e is not None]
    entries.sort(key=lambda e: e.get("started_at") or 0)
    return entries[-VOICE_MAX_CHAT_CALL_INDEX:]


def _chat_call_index(db) -> list:
    """Every chat's kept calls, oldest first; read from the calls once on a
    host whose saves predate the list, and kept from then on."""
    entries = _stored_chat_call_index(db)
    if entries is not None:
        return entries
    entries = _chat_calls_on_disk()
    if entries is None:
        return []  # nothing written, so the next read tries again
    if hasattr(db, "set_meta"):
        try:
            with _voice_lock():
                if _stored_chat_call_index(db) is None:
                    _write_chat_call_index(db, entries)
        except Exception:  # noqa: BLE001 — the list is still answered; the next read writes it
            logger.warning("Conduit voice: couldn't keep the chat call list", exc_info=True)
    return entries


def _drop_chat_calls(db, chat_id: str) -> None:
    """A gone chat's calls and list (Hermes has no meta delete)."""
    ids = _chat_call_ids(db, chat_id)
    for call_id in ids:
        call = _chat_call(db, call_id)
        # A listed call whose body is gone or unreadable goes too.
        if call is None or call["chat_id"] == chat_id:
            db.set_meta(VOICE_CHAT_CALL_KEY.format(call_id=call_id), "")
    if ids:
        db.set_meta(VOICE_CHAT_CALLS_KEY.format(chat_id=chat_id), "")
    _unindex_chat_calls(db, lambda entry: entry["chat_id"] == chat_id)


def _save_chat_call_turns(chat_id: str, call_id: str, engine: str, turns: list,
                          on_end: Optional[Callable[..., None]]) -> Dict[str, Any]:
    """Store an attached call's turns with its chat; same index rules as a row.

    No session is made and nothing is added to the chat's messages. The call
    keeps the chat it was first saved to."""
    with _voice_lock():
        db = _open_voice_db(_VOICE_WRITE)
        try:
            row = db.get_session(chat_id)
            if not row:
                try:
                    _drop_chat_calls(db, chat_id)
                except Exception:  # noqa: BLE001 — the 422 must win; the next save retries
                    logger.warning("Couldn't clear gone chat %s's calls", chat_id, exc_info=True)
                raise core.TokenError(422, "That chat no longer exists; save the call on its own")
            if _is_voice_row(db, chat_id, row):
                raise core.TokenError(422, "That is a saved call, not a chat; save the call on its own")
            call = _chat_call(db, call_id)
            if call is not None and call["chat_id"] != chat_id:
                raise core.TokenError(400, "That call belongs to another chat")
            stored = call["turns"] if call else []
            # The call keeps the engine it was first saved with, as a row does.
            engine = (call or {}).get("engine") or engine
            last = len(stored) - 1
            fresh = []
            for turn in turns:  # sorted by index
                if turn["index"] <= last:
                    continue
                if turn["index"] != last + 1:
                    raise core.TokenError(400, f"Turns must continue from index {last + 1} for this call")
                fresh.append(turn)
                last = turn["index"]
            messages = [{k: v for k, v in turn.items() if k != "index" and v is not None} for turn in fresh]
            if len(stored) + len(messages) > VOICE_MAX_CHAT_CALL_TURNS:
                raise core.TokenError(413, f"A call kept with a chat holds at most {VOICE_MAX_CHAT_CALL_TURNS} turns")
            if messages or call is not None:
                # The chat's list first, and on every save of a stored call: a
                # failure between the two writes never hides the call from it.
                ids = _chat_call_ids(db, chat_id)
                if call_id not in ids:
                    ids.append(call_id)
                    evicted = ids[:-VOICE_MAX_CHAT_CALLS] if len(ids) > VOICE_MAX_CHAT_CALLS else []
                    ids = ids[len(evicted):]
                    db.set_meta(VOICE_CHAT_CALLS_KEY.format(chat_id=chat_id), json.dumps(ids, separators=(",", ":")))
                    # Only once the list no longer names them.
                    for old in evicted:
                        db.set_meta(VOICE_CHAT_CALL_KEY.format(call_id=old), "")
                    if evicted:
                        _unindex_chat_calls(db, lambda entry: entry["call_id"] in evicted)
            if messages:
                db.set_meta(VOICE_CHAT_CALL_KEY.format(call_id=call_id), json.dumps(
                    {"chat_id": chat_id, "engine": engine, "turns": stored + messages},
                    separators=(",", ":")))
                _index_chat_call(db, call_id, chat_id, engine, stored + messages)
            elif call is not None:
                # A retry after a failed list write: the turns are stored, so
                # the call is listed now.
                listed = _stored_chat_call_index(db)
                if listed is None or all(e["call_id"] != call_id for e in listed):
                    _index_chat_call(db, call_id, chat_id, engine, stored)
            result = {"session_id": None, "chat_id": chat_id, "written": last + 1, "appended": len(messages),
                      "skipped": len(turns) - len(fresh), "created": False}
        finally:
            db.close()
        if messages and on_end is not None:
            on_end(chat_id, engine, messages, chat_call=True)
    return result


def voice_chat_calls(chat_id: Any, only: Any = None) -> Dict[str, Any]:
    """An attached chat's saved calls, oldest first; none once the chat is gone
    (its calls are cleared on the next save to it). Turns come only with
    *only*, one call, so a response holds at most one call's turns."""
    chat_id = _voice_id(chat_id, "chat_id")
    only = _voice_id(only, "call_id") if only is not None else None
    db = _open_voice_db(("get_meta", "get_session"))
    try:
        calls = []
        for call_id in _chat_call_ids(db, chat_id) if db.get_session(chat_id) else []:
            if only is not None and call_id != only:
                continue
            call = _chat_call(db, call_id)
            if call is None or call["chat_id"] != chat_id:
                continue
            entry = {"call_id": call_id, "engine": call.get("engine")}
            if only is not None:
                entry["turns"] = [_chat_call_turn(t) for t in call["turns"] if isinstance(t, dict)]
            calls.append(entry)
    finally:
        db.close()
    return {"chat_id": chat_id, "calls": calls}


def _chat_call_turn(turn: Dict[str, Any]) -> Dict[str, Any]:
    entry = {"role": turn.get("role"), "text": turn.get("content"), "at": turn.get("timestamp")}
    schema = _card_schema() if turn.get("cards") else None
    if schema:
        # Checked again: the stored list is only as good as the file.
        cards = schema.normalize_cards(turn["cards"])
        if cards:
            entry["cards"] = cards
    return entry


def set_voice_tag(body: Dict[str, Any]) -> Dict[str, Any]:
    session_id = _voice_id(body.get("session_id"), "session_id")
    kind = body.get("kind")
    if kind not in VOICE_TAG_KINDS - {"call"}:
        raise core.TokenError(400, "kind must be classic or job")
    tag: Dict[str, Any] = {"kind": kind}
    if body.get("parent_id"):
        tag["parent_id"] = _voice_id(body.get("parent_id"), "parent_id")
    parent_title = _voice_text(body.get("parent_title"), VOICE_MAX_TITLE_CHARS)
    if parent_title:
        tag["parent_title"] = parent_title
    with _voice_lock():
        db = _open_voice_db(_VOICE_WRITE)
        try:
            if _voice_tags(db).get(session_id, {}).get("kind") == "call":
                raise core.TokenError(422, "That session is a saved voice call; its tag can't change")
            _set_voice_tag(db, session_id, tag)
            # A live call's jobs are tagged when the call ends, often after
            # Hermes answered them: one that's done leaves Desktop's list
            # now, as it would have at its turn's end.
            if kind == "job":
                _archive_settled_job(db, session_id)
        finally:
            db.close()
    return {"session_id": session_id, **tag}


def _archive_settled_job(db, session_id: str) -> bool:
    try:
        return chats._voice_archive().archive_settled_job(db, session_id)
    except Exception:  # noqa: BLE001 — the tag is stored; the chat just stays listed
        logger.warning("Couldn't archive Conduit voice job %s", session_id, exc_info=True)
        return False


def archive_classic_voice_chat(body: Dict[str, Any]) -> Dict[str, Any]:
    """A Classic voice chat a call started, archived when that call ends,
    like a live call's own row (hermes-conduit#1124). The chat may still be
    open in Conduit; the next turn anyone runs in it un-archives it (the
    pre_llm_call hook), as Hermes does for a reopened row."""
    session_id = _voice_id(body.get("session_id"), "session_id")
    with _voice_lock():
        db = _open_voice_db(_VOICE_WRITE)
        try:
            row = db.get_session(session_id)
            if not row:
                raise core.TokenError(422, "That chat no longer exists")
            if _voice_tags(db).get(session_id, {}).get("kind") != "classic":
                raise core.TokenError(422, "That chat isn't one a Classic voice call started")
            # A pin is an explicit keep; an archived row stays as it is.
            archived = not row.get("pinned") and not row.get("archived") \
                and _archive_ended_voice_row(db, session_id)
        finally:
            db.close()
    return {"session_id": session_id, "archived": bool(archived)}


def voice_tags() -> Dict[str, Any]:
    db = _open_voice_db()
    try:
        tags, calls = _voice_tags(db), _chat_call_index(db)
        # A chat deleted in Desktop leaves no save to clear its calls: they
        # go here, the first time a device asks.
        gone = {chat_id for chat_id in {e["chat_id"] for e in calls} if not db.get_session(chat_id)} \
            if hasattr(db, "get_session") else set()
    finally:
        db.close()
    if gone:
        _drop_gone_chats_calls(gone)
    return {"tags": tags, "chat_calls": [e for e in calls if e["chat_id"] not in gone]}


def _drop_gone_chats_calls(chat_ids: set) -> None:
    try:
        with _voice_lock():
            db = _open_voice_db(_VOICE_WRITE)
            try:
                for chat_id in chat_ids:
                    if not db.get_session(chat_id):
                        _drop_chat_calls(db, chat_id)
            finally:
                db.close()
    except Exception:  # noqa: BLE001 — they're left out of the answer anyway; the next read retries
        logger.warning("Conduit voice: couldn't clear gone chats' calls", exc_info=True)


def voice_summary(session_id: Any) -> Dict[str, Any]:
    session_id = _voice_id(session_id, "session_id")
    db = _open_voice_db()
    try:
        raw = db.get_meta(VOICE_SUMMARY_KEY.format(session_id=session_id))
    finally:
        db.close()
    try:
        stored = json.loads(raw) if raw else {}
    except ValueError:
        stored = {}
    text = str(stored.get("text") or "") if isinstance(stored, dict) else ""
    covers = stored.get("covers") if isinstance(stored, dict) else None
    return {"available": bool(text), "text": text, "covers": covers if isinstance(covers, int) else 0}


def set_voice_summary(body: Dict[str, Any]) -> Dict[str, Any]:
    session_id = _voice_id(body.get("session_id"), "session_id")
    text = memory._clip_block(body.get("text"), VOICE_MAX_SUMMARY_CHARS) if isinstance(body.get("text"), str) else ""
    covers = body.get("covers")
    if not text or not isinstance(covers, int) or isinstance(covers, bool) or covers < 0:
        raise core.TokenError(400, "A summary needs text and a non-negative covers count")
    with _voice_lock():
        db = _open_voice_db(_VOICE_WRITE)
        try:
            row = db.get_session(session_id)
            if not row:
                raise core.TokenError(422, "That voice session no longer exists; start a new one")
            if not _is_voice_row(db, session_id, row):
                raise core.TokenError(422, "That session isn't a saved voice call")
            db.set_meta(VOICE_SUMMARY_KEY.format(session_id=session_id),
                        json.dumps({"text": text, "covers": covers}, separators=(",", ":")))
        finally:
            db.close()
    return {"session_id": session_id, "covers": covers}


def _voice_limit(write: bool) -> None:
    """Checked first, before a body is read or a worker is taken. One budget
    for the whole dashboard: the profile isn't resolved yet, and keying on the
    raw query value would let made-up names mint fresh windows."""
    try:
        (_voice_write_limiter if write else _voice_read_limiter).acquire("")
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})


async def _run_voice(profile: Optional[str], fn: Callable[[], Dict[str, Any]], what: str) -> Dict[str, Any]:
    no_store = {"Cache-Control": "no-store"}
    try:
        return await asyncio.wait_for(core._run_scoped(profile, fn, _voice_executor), timeout=VOICE_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.warning("Voice %s for Conduit timed out after %ss", what, VOICE_TIMEOUT_S)
        raise HTTPException(status_code=504, detail=f"Voice {what} timed out", headers=no_store)
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers=no_store)
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), **no_store}
        raise
    except Exception as exc:
        # Hermes' write guards: a compression in flight is transient (Conduit
        # keeps the turns and retries); a row that compression already closed
        # never takes appends again, so Conduit starts a new row instead.
        names = {cls.__name__ for cls in type(exc).__mro__}
        if "CompressionSessionClosedError" in names:
            raise HTTPException(status_code=422, detail="That session was compacted; start a new one", headers=no_store)
        if "CompressionSessionBusyError" in names:  # includes SessionCompressionInProgressError
            raise HTTPException(status_code=409, detail="The session is busy; try again shortly", headers=no_store)
        raise core._unexpected(what, exc, feature="Voice")


async def _voice_body(request: Request) -> Dict[str, Any]:
    body = await core._read_json_body(request, VOICE_MAX_BODY_BYTES)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object",
                            headers={"Cache-Control": "no-store"})
    return body


@router.post("/voice/sessions")
async def post_voice_session(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    _voice_limit(write=True)
    body = await _voice_body(request)

    def ended(session_id: str, engine: str, messages: list, chat_call: bool = False) -> None:
        _queue_voice_call_end(profile, session_id, engine, messages, chat_call=chat_call)

    return {"ok": True, **(await _run_voice(profile, lambda: save_voice_turns(body, on_end=ended), "save"))}


@router.get("/voice/chat-calls")
async def get_voice_chat_calls(response: Response, chat_id: Optional[str] = None,
                               call_id: Optional[str] = None,
                               profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    _voice_limit(write=False)
    return {"ok": True, **(await _run_voice(profile, lambda: voice_chat_calls(chat_id, call_id), "chat calls"))}


@router.get("/voice/tags")
async def get_voice_tags(response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    _voice_limit(write=False)
    return {"ok": True, **(await _run_voice(profile, voice_tags, "tags"))}


@router.post("/voice/tags")
async def post_voice_tag(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    _voice_limit(write=True)
    body = await _voice_body(request)
    return {"ok": True, **(await _run_voice(profile, lambda: set_voice_tag(body), "tag"))}


@router.post("/voice/archive")
async def post_voice_archive(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    _voice_limit(write=True)
    body = await _voice_body(request)
    return {"ok": True, **(await _run_voice(profile, lambda: archive_classic_voice_chat(body), "archive"))}


@router.get("/voice/summary")
async def get_voice_summary(response: Response, session_id: Optional[str] = None,
                            profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    _voice_limit(write=False)
    return {"ok": True, **(await _run_voice(profile, lambda: voice_summary(session_id), "summary"))}


@router.post("/voice/summary")
async def post_voice_summary(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    _voice_limit(write=True)
    body = await _voice_body(request)
    return {"ok": True, **(await _run_voice(profile, lambda: set_voice_summary(body), "summary"))}


# --- Call end ------------------------------------------------------------------
#
# A live call never runs a Hermes turn, so Hermes never learns it is over: no
# session-end hook fires and the memory provider (Honcho, Mem0, …) never sees
# the conversation (hermes-conduit#416). Conduit saves a call only once it has
# ended (hung up, dropped or timed out), so a save that stores new turns is
# that call's end. The plugin then ends the session the way Hermes ends a
# Desktop chat (tui_gateway's _finalize_session): the on_session_end hook, the
# memory provider's sync_turn per exchange and its on_session_end, then
# on_session_finalize. A retried save that stores nothing new ends nothing.
#
# It runs off the request, one call at a time, so a slow provider never holds
# up a save. Memory gets only the call's new turns: a resumed call's earlier
# turns went there when their own call ended.
# Hermes runs a "desktop" row as the desktop platform.
VOICE_END_PLATFORM = VOICE_SESSION_SOURCE
VOICE_END_HOOK_REASON = "voice_call_ended"
# Past this a wedged hook or provider is left running, so the next call still ends.
VOICE_END_TIMEOUT_S = 120.0
# The wait for queued sync_turn writes before the provider's on_session_end.
VOICE_END_FLUSH_TIMEOUT_S = 30.0
# Calls waiting behind a slow one; past this a call's end is skipped (and logged).
VOICE_END_MAX_PENDING = 20
# Ends left running past VOICE_END_TIMEOUT_S in one profile; past this that
# profile's calls end without these steps (and logged) until one finishes.
VOICE_END_MAX_ABANDONED = 3

_VOICE_END_MODULES = memory._MISSING_MEMORY_MODULES | {"hermes_cli.lifecycle", "hermes_cli.plugins", "agent.memory_manager"}

# A daemon worker, not an executor: an executor's worker is joined at exit,
# so a wedged provider could hold up a gateway restart. An end still running
# when the dashboard exits is dropped, as Hermes drops its own memory writes.
_voice_end_jobs: "queue.SimpleQueue[Callable[[], None]]" = queue.SimpleQueue()
_voice_end_worker: Optional[threading.Thread] = None
_voice_end_guard = threading.Lock()
_voice_end_pending = 0
_voice_end_abandoned: Dict[str, int] = {}


def _voice_exchanges(messages: list) -> list:
    """(user, assistant) pairs as Hermes syncs a turn: what the user said, then
    everything said back. Hermes skips a turn missing either side, so a greeting
    before the user spoke and an unanswered last question stay out."""
    exchanges = []
    user: list = []
    assistant: list = []
    for message in messages:
        if message["role"] == "user":
            if assistant:
                if user:
                    exchanges.append(("\n".join(user), "\n".join(assistant)))
                user, assistant = [], []
            user.append(message["content"])
        else:
            assistant.append(message["content"])
    if user and assistant:
        exchanges.append(("\n".join(user), "\n".join(assistant)))
    return exchanges


def _fire_voice_hook(name: str, **kwargs: Any) -> None:
    """Fire a lifecycle hook as Hermes does: its own observers, then plugins.
    on_session_finalize goes through finalize_session, which also closes the
    session's Relay conversation. A Hermes without hermes_cli.lifecycle calls
    the plugin hooks directly."""
    try:
        from hermes_cli import lifecycle
    except ImportError as exc:
        if exc.name not in ("hermes_cli", "hermes_cli.lifecycle"):
            raise
        from hermes_cli.plugins import invoke_hook

        invoke_hook(name, **kwargs)
        return
    if name == "on_session_finalize" and hasattr(lifecycle, "finalize_session"):
        lifecycle.finalize_session(**kwargs)
    else:
        lifecycle.invoke_hook(name, **kwargs)


def _voice_session_title(session_id: str) -> Dict[str, str]:
    """The row's title and its provenance, which Hermes hands a provider too
    (Honcho can name its session after the title). Empty when unreadable."""
    try:
        db = _open_voice_db()
    except Exception:  # noqa: BLE001 — the title is optional
        return {}
    try:
        title = db.get_session_title(session_id) if hasattr(db, "get_session_title") else None
        if not title:
            return {}
        found = {"session_title": title}
        source = db.get_session_title_source(session_id) if hasattr(db, "get_session_title_source") else None
        if source:
            found["session_title_source"] = source
        return found
    except Exception:  # noqa: BLE001
        logger.debug("Couldn't read the title of Conduit voice session %s", session_id, exc_info=True)
        return {}
    finally:
        db.close()


def _commit_voice_call_memory(session_id: str, messages: list, end_session: bool = True) -> None:
    """Write a finished call to the profile's memory provider the way Hermes
    writes a chat: initialize it for the session, sync_turn each exchange, then
    on_session_end and shutdown. A provider instance of its own: the cached
    recall one is bound to the conduit-voice session. A call kept with a chat
    passes end_session=False: the chat goes on, and a remote provider keyed by
    session id would take on_session_end as the chat's own end."""
    name = memory._configured_memory_provider(memory._hermes_memory_config())
    if name is None:
        return
    from agent.memory_manager import MemoryManager

    provider = memory._hermes_load_memory_provider(name)
    if provider is None or not provider.is_available():
        logger.info("Memory provider %r is not available; a Conduit voice call isn't written to it", name)
        return
    manager = MemoryManager()
    manager.add_provider(provider)
    kwargs = {**memory._memory_provider_init_kwargs(), "platform": VOICE_END_PLATFORM, **_voice_session_title(session_id)}
    try:
        manager.initialize_all(session_id=session_id, **kwargs)
        for user, assistant in _voice_exchanges(messages):
            manager.sync_all(user, assistant, session_id=session_id)
        # sync_all writes on the manager's worker: let those land before the end.
        flush = getattr(manager, "flush_pending", None)
        if callable(flush):
            flush(timeout=VOICE_END_FLUSH_TIMEOUT_S)
        if end_session:
            manager.on_session_end([{"role": m["role"], "content": m["content"]} for m in messages])
    finally:
        manager.shutdown_all()


def _end_voice_call(session_id: str, engine: str, messages: list, chat_call: bool = False) -> None:
    """End a saved call's session in Hermes (inside the profile's scope). Each
    step runs even if an earlier one failed. A call attached to a chat only
    syncs its turns into memory under the chat's session: the chat goes on, so
    neither its session nor its memory session is ended."""
    hook = {"session_id": session_id, "platform": VOICE_END_PLATFORM, "reason": VOICE_END_HOOK_REASON}
    memory = ("memory write", lambda: _commit_voice_call_memory(session_id, messages, end_session=not chat_call))
    steps = (memory,) if chat_call else (
        ("on_session_end hook", lambda: _fire_voice_hook(
            "on_session_end", completed=True, interrupted=False, model=engine, **hook)),
        memory,
        ("on_session_finalize hook", lambda: _fire_voice_hook("on_session_finalize", **hook)),
    )
    for what, step in steps:
        try:
            step()
        except ImportError as exc:
            if exc.name not in _VOICE_END_MODULES:
                logger.warning("Conduit voice call end: %s failed for %s", what, session_id, exc_info=True)
            else:
                logger.debug("Conduit voice call end: this Hermes has no %s (%s)", what, exc.name)
        except Exception:  # noqa: BLE001 — a hook or provider must never stop the rest
            logger.warning("Conduit voice call end: %s failed for %s", what, session_id, exc_info=True)


def _run_voice_call_end(profile: Optional[str], session_id: str, engine: str, messages: list,
                        chat_call: bool = False) -> None:
    """Runs the end on its own thread and waits up to VOICE_END_TIMEOUT_S, so a
    wedged provider costs one abandoned thread, not every later call's end.

    The next call's end can then overlap the abandoned one, each with its own
    provider instance. Hermes' gateway does the same (one provider per agent,
    abandoned on a cleanup timeout), so providers already allow it. A profile
    keeps at most VOICE_END_MAX_ABANDONED of them, so a provider that never
    returns can't pile up threads, and other profiles' calls still end."""
    key = profile or ""
    with _voice_end_guard:
        if _voice_end_abandoned.get(key, 0) >= VOICE_END_MAX_ABANDONED:
            logger.warning("Conduit voice session %s ends without hooks: earlier call ends in this profile "
                           "are still stuck", session_id)
            return
    done = threading.Event()
    abandoned = False

    def run() -> None:
        try:
            with core._profile_scope(profile):
                _end_voice_call(session_id, engine, messages, chat_call=chat_call)
        except Exception:  # noqa: BLE001 — the scope itself failed; nothing ran
            logger.warning("Conduit voice call end failed for %s", session_id, exc_info=True)
        finally:
            with _voice_end_guard:
                done.set()
                if abandoned:
                    _voice_end_abandoned[key] -= 1
                    if not _voice_end_abandoned[key]:
                        del _voice_end_abandoned[key]

    try:
        threading.Thread(target=run, name="conduit-voice-end-call", daemon=True).start()
    except RuntimeError:  # no thread to spare
        logger.warning("Conduit voice session %s ends without hooks: no thread to run them", session_id)
        return
    if not done.wait(VOICE_END_TIMEOUT_S):
        with _voice_end_guard:
            if not done.is_set():
                abandoned = True
                _voice_end_abandoned[key] = _voice_end_abandoned.get(key, 0) + 1
                logger.warning("Ending Conduit voice session %s took over %ss; moving on (it may still finish)",
                               session_id, VOICE_END_TIMEOUT_S)


def _voice_end_loop() -> None:
    while True:
        job = _voice_end_jobs.get()
        try:
            job()
        except Exception:  # noqa: BLE001 — one call's end never stops the next
            logger.warning("A Conduit voice call end failed", exc_info=True)


def _queue_voice_call_end(profile: Optional[str], session_id: str, engine: str, messages: list,
                          chat_call: bool = False) -> None:
    """Ends the call's session off the request. Never raises: the turns are
    already stored, and a failed save would make Conduit send them again."""
    try:
        _enqueue_voice_call_end(profile, session_id, engine, messages, chat_call)
    except Exception:  # noqa: BLE001
        logger.warning("Conduit voice session %s ends without hooks: queueing failed", session_id, exc_info=True)


def _enqueue_voice_call_end(profile: Optional[str], session_id: str, engine: str, messages: list,
                            chat_call: bool = False) -> None:
    global _voice_end_pending, _voice_end_worker

    def run() -> None:
        global _voice_end_pending
        try:
            _run_voice_call_end(profile, session_id, engine, messages, chat_call)
        finally:
            with _voice_end_guard:
                _voice_end_pending -= 1

    with _voice_end_guard:
        if _voice_end_pending >= VOICE_END_MAX_PENDING:
            logger.warning("Too many Conduit voice calls waiting to end; session %s ends without hooks", session_id)
            return
        try:
            if _voice_end_worker is None or not _voice_end_worker.is_alive():
                _voice_end_worker = threading.Thread(target=_voice_end_loop, name="conduit-voice-end", daemon=True)
                _voice_end_worker.start()
        except RuntimeError:  # no thread to spare, or the dashboard is exiting
            logger.warning("Conduit voice session %s ends without hooks: no thread to run them", session_id)
            return
        # Counted once queued, so a failed put holds no slot (the worker's
        # count down waits for this guard).
        _voice_end_jobs.put(run)
        _voice_end_pending += 1
