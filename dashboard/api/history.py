"""Chat history: a page of a chat's rows, without the ones Conduit already has."""

from __future__ import annotations

import hashlib
import json
import sys
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, Request, Response

from . import core

logger = core.logger
router = core.router


# --- Chat history pages (hermes-conduit#1525) ---------------------------------
#
# Conduit opens a chat with Hermes' own page of its newest rows
# (GET /api/sessions/{id}/messages?order=latest). On a slow link most of the
# wait is that page's bytes, even when the phone already holds every row of
# it. This route reads the same page through Hermes' own route function, so
# it is that answer exactly, and sends each row the phone says it holds as its
# fingerprint alone. Conduit puts its copy of those rows back in place, so
# what it reads is Hermes' page row for row: an edit, an /undo or a compaction
# changes a row's fingerprint, and that row comes in full.
#
# Hermes' keyset paging (get_messages(after_id=...)) can't do this: it refuses
# the compacted and lineage reads the page is made from.

HISTORY_MAX_BODY_BYTES = 64 * 1024
HISTORY_MAX_KNOWN = 500
HISTORY_DIGEST_CHARS = 24
# The key a held row is sent under, in place of the row.
HISTORY_KNOWN_KEY = "conduit_known"


def row_digest(row: Any) -> str:
    """A row's fingerprint: its content, keys sorted, so the same row always
    hashes the same."""
    text = json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:HISTORY_DIGEST_CHARS]


def _session_routes() -> Any:
    sessions = sys.modules.get("hermes_cli.web_routers.sessions")
    if sessions is None:
        try:
            from hermes_cli.web_routers import sessions
        except Exception as exc:  # noqa: BLE001 — Conduit reads Hermes' own route instead
            if not isinstance(exc, ImportError):
                logger.warning("Hermes' session routes failed to load: %s", type(exc).__name__)
            return None
    return sessions if hasattr(sessions, "get_session_messages") else None


def _known_digests(body: Any) -> set:
    known = body.get("known") if isinstance(body, dict) else None
    if not isinstance(known, list):
        return set()
    return {item for item in known[:HISTORY_MAX_KNOWN]
            if isinstance(item, str) and len(item) == HISTORY_DIGEST_CHARS}


def history_page(page: Dict[str, Any], known: set) -> Dict[str, Any]:
    """Hermes' page with each row ``known`` holds sent as its fingerprint, and
    every row's fingerprint in ``row_digests``."""
    rows = page.get("messages")
    if not isinstance(rows, list):
        return page
    digests: List[str] = []
    sent: List[Any] = []
    for row in rows:
        digest = row_digest(row)
        digests.append(digest)
        sent.append({HISTORY_KNOWN_KEY: digest} if digest in known else row)
    return {**page, "messages": sent, "row_digests": digests}


@router.post("/sessions/history-page")
async def post_history_page(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    profile = await core._conduit_route_profile_no_store(request, profile)
    body = await core._read_json_body(request, HISTORY_MAX_BODY_BYTES)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object")
    session_id = body.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise HTTPException(status_code=400, detail="session_id is required")
    limit = body.get("limit")
    offset = body.get("offset", 0)
    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 0):
        raise HTTPException(status_code=400, detail="limit must be a whole number")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise HTTPException(status_code=400, detail="offset must be a whole number")
    routes = _session_routes()
    if routes is None:
        raise HTTPException(status_code=501, detail="Reading history pages needs a newer Hermes on this host.")
    # Hermes' own route answers the 400s and 404s Conduit already reads.
    page = await routes.get_session_messages(
        session_id, profile=profile, limit=limit, offset=offset, order=body.get("order"),
        include_compacted=bool(body.get("include_compacted", False)),
        inline_images=bool(body.get("inline_images", True)))
    if not isinstance(page, dict):
        raise core._unexpected("history-page", TypeError("not a JSON object"), feature="Chat history")
    return history_page(page, _known_digests(body))
