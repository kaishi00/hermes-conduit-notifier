"""Voice chats the plugin archived come back once they're used (hermes-conduit#1124).

A standalone call's row, and a Classic chat a voice call started, are archived
with Hermes' idle-sweep stamp (``auto_archived``) when the call ends. Hermes
un-archives such a row when a turn reopens it, but a Classic chat can still be
open when its call ends, and then nothing reopens it: the next turn there runs
in a chat Desktop's list doesn't show. The pre_llm_call hook calls
``bring_back`` with the turn's session, which un-archives it the way Hermes'
reopen does (a lineage someone archived on purpose stays archived).

A voice job's chat is archived the same way once Hermes has answered what
voice asked in it (``archive_answered_job``, from on_session_end, and when
Conduit tags the job after its call): only while every request in it came
from voice, so a chat someone typed into stays listed. Conduit lists these
chats with their calls whatever Desktop shows.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

STATE_DB = "state.db"
# Kept in step with dashboard/plugin_api.py.
VOICE_TAGS_KEY = "conduit.voice.tags"
ARCHIVED_KINDS = frozenset({"call", "classic", "job"})
# How a request voice sent reads at its start: a job's first prompt and the
# Watch's later ones (Conduit's VoiceBackgroundJobSupervisor.jobPrompt,
# dashboard/plugin_api.py's WATCH_JOB_PROMPT and WATCH_JOB_NEXT_PROMPT), and
# a call's words in a chat or a job ("(voice) ", threadTurnText).
_VOICE_REQUEST_RE = re.compile(
    r"^\s*(?:\[Background job started from a Conduit voice conversation\."
    r"|\[The next request from the same Conduit voice conversation\."
    r"|\(voice\) )")
# Enough of a message to see its mark.
_MARK_CHARS = 120


def _archived_voice_chat(path: Path, session_id: str) -> bool:
    """One read-only look: the row carries the sweep's stamp and is a voice
    chat. Most turns stop at the first query."""
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=1.0)
    try:
        row = conn.execute("SELECT archived, auto_archived FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not row or not row[0] or not row[1]:
            return False
        meta = conn.execute("SELECT value FROM state_meta WHERE key = ?", (VOICE_TAGS_KEY,)).fetchone()
    finally:
        conn.close()
    try:
        tags = json.loads(meta[0]) if meta and meta[0] else {}
    except ValueError:
        return False
    tag = tags.get(session_id) if isinstance(tags, dict) else None
    return isinstance(tag, dict) and tag.get("kind") in ARCHIVED_KINDS


def _open_session_db(path: Path):
    from hermes_state import SessionDB

    return SessionDB(db_path=path)


def bring_back(home: Path, session_id: str, open_db: Callable[[Path], object] = _open_session_db) -> bool:
    """Un-archives ``session_id`` when the plugin archived it as a voice chat.
    True when it did. Never raises: a hook must not break the turn."""
    if not session_id:
        return False
    path = Path(home) / STATE_DB
    try:
        if not path.exists() or not _archived_voice_chat(path, session_id):
            return False
        db = open_db(path)
    except Exception:  # noqa: BLE001
        logger.debug("conduit_push: couldn't check whether voice chat %s was archived", session_id, exc_info=True)
        return False
    try:
        write: Optional[Callable] = getattr(db, "_execute_write", None)
        unarchive: Optional[Callable] = getattr(db, "_unarchive_auto_archived_lineage", None)
        if write is None or unarchive is None:
            return False
        # Its own result isn't kept: sqlite3 may not count a CTE update's rows.
        write(lambda conn: unarchive(conn, session_id))
        return True
    except Exception:  # noqa: BLE001
        logger.warning("conduit_push: couldn't un-archive voice chat %s", session_id, exc_info=True)
        return False
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            logger.debug("conduit_push: closing the session store failed", exc_info=True)


def is_voice_request(content: object) -> bool:
    """A user message voice sent, by its mark."""
    return isinstance(content, str) and _VOICE_REQUEST_RE.match(content[:_MARK_CHARS]) is not None


def _tag(raw: Optional[str], session_id: str) -> Optional[dict]:
    try:
        tags = json.loads(raw) if raw else {}
    except ValueError:
        return None
    tag = tags.get(session_id) if isinstance(tags, dict) else None
    return tag if isinstance(tag, dict) else None


def settled_voice_job(db, session_id: str) -> bool:
    """``session_id`` is a voice job's chat (its tag) that voice alone has
    used, and Hermes has answered its latest request: every user message
    carries a voice mark and the last word is Hermes'. One pinned, archived
    or gone is left as it is. *db* is a SessionDB."""
    tag = _tag(db.get_meta(VOICE_TAGS_KEY), session_id)
    if not tag or tag.get("kind") != "job":
        return False
    row = db.get_session(session_id)
    if not row or row.get("pinned") or row.get("archived"):
        return False
    messages = [m for m in db.get_messages(session_id)
                if isinstance(m, dict) and m.get("role") in ("user", "assistant", "tool")]
    asked = [m for m in messages if m.get("role") == "user"]
    if not asked or not all(is_voice_request(m.get("content")) for m in asked):
        return False
    # An answer that calls tools ("Let me check") is mid-turn, not the last word.
    answer = messages[-1]
    return answer.get("role") == "assistant" and not answer.get("tool_calls") \
        and isinstance(answer.get("content"), str) and bool(answer["content"].strip())


def archive_settled_job(db, session_id: str) -> bool:
    """Archives a settled voice job's chat with the idle sweep's stamp, so a
    turn in it brings it back. True when it did. A Hermes without the
    stamp keeps it listed."""
    archive: Optional[Callable] = getattr(db, "_auto_archive_lineage", None)
    if archive is None or not hasattr(db, "get_messages") or not settled_voice_job(db, session_id):
        return False
    archive(session_id)
    return True


def _may_be_voice_job(path: Path, session_id: str) -> bool:
    """One read-only look before the store is opened for writes: the chat's
    first request is voice's and it's tagged a job. Most turns stop at the
    first query."""
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=1.0)
    try:
        first = conn.execute("SELECT substr(content, 1, ?) FROM messages WHERE session_id = ? AND role = 'user' "
                             "ORDER BY id LIMIT 1", (_MARK_CHARS, session_id)).fetchone()
        if not first or not is_voice_request(first[0]):
            return False
        meta = conn.execute("SELECT value FROM state_meta WHERE key = ?", (VOICE_TAGS_KEY,)).fetchone()
    finally:
        conn.close()
    tag = _tag(meta[0] if meta else None, session_id)
    return bool(tag) and tag.get("kind") == "job"


def archive_answered_job(home: Path, session_id: str, open_db: Callable[[Path], object] = _open_session_db) -> bool:
    """At the end of a turn Hermes answered: archives ``session_id`` when
    it's a settled voice job (``settled_voice_job``). True when it did.
    Never raises: a hook must not break the turn."""
    if not session_id:
        return False
    path = Path(home) / STATE_DB
    try:
        if not path.exists() or not _may_be_voice_job(path, session_id):
            return False
        db = open_db(path)
    except Exception:  # noqa: BLE001
        logger.debug("conduit_push: couldn't check whether %s is a voice job", session_id, exc_info=True)
        return False
    try:
        return archive_settled_job(db, session_id)
    except Exception:  # noqa: BLE001 — the chat just stays listed
        logger.warning("conduit_push: couldn't archive voice job %s", session_id, exc_info=True)
        return False
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            logger.debug("conduit_push: closing the session store failed", exc_info=True)
