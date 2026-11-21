"""Hermes is waiting on you: every chat of this profile with an approval or
a question waiting on the user, for Conduit's "Needs you" list, the counts
on its profile strip and its in-app banner (hermes-conduit issue 1705).

The waits are the ones the hooks keep in the profile's conduit-calls.json
(calls_store.note_waiting), for every chat now, whether or not alert calls
are on. The list only reads: an approval is answered on its chat's own card,
and a question on the card Conduit draws from the row's ``decision`` when
Hermes drew none in the chat (hermes-conduit issue 2556). ``questions`` are
the ones that wait past their turn (work_propose, and a clarify question kept
after it ran out), answered through the Work panel's question route. The list names the
profile's relay gateway (``gateway_id``), so that card's answer is sealed for
the pairing like a notification's (hermes-conduit issue 2683).
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
    """A bot's workstreams, whose Work panel answers their approvals; none
    when the registry can't be read."""
    try:
        return core._plugin_package_module("work_store").Store(Path(home)).session_ids()
    except Exception:  # noqa: BLE001 — an unreadable registry leaves nothing out
        return []


def _open_questions(home: Any) -> list:
    """The questions the bot asked that wait on the user's answer past their
    turn (work_propose, and clarify questions kept after they ran out), each
    as its card: answered with a tap through ``/workstreams/<id>/...``. A
    ``chat`` of "" is the Bot Chat's. None when the registry can't be read."""
    try:
        work_store = core._plugin_package_module("work_store")
        if not work_store.exists(Path(home)):
            return []
        work_coord = core._plugin_package_module("work_coord")
        store = work_store.Store(Path(home))
        rows = work_coord.open_questions(store)
        # Each card names the work it's about, as the Work panel's do.
        titles = store.titles([ref for row in rows for ref in work_store.proposal_refs(row)])
        cards = [work_coord.question_card(row, titles) for row in rows]
    except Exception:  # noqa: BLE001 — the waits still list
        return []
    bot_chat = ""
    if any(not card["chat"] for card in cards):
        try:
            bot_chat = core._plugin_package_module("work_tool")._bot_chat_of(Path(home))
        except Exception:  # noqa: BLE001 — the row then names no chat to open
            bot_chat = ""
    try:
        titles = core._plugin_package_module("session_titles").title
    except Exception:  # noqa: BLE001 — rows without a title still list
        titles = None
    for card in cards:
        # The chat its row opens: the one that asked, or the Bot Chat.
        card["session_ids"] = [chat for chat in (card["chat"] or bot_chat,) if chat]
        try:
            card["chat_title"] = titles(card["session_ids"][0]) if titles and card["session_ids"] else ""
        except Exception:  # noqa: BLE001
            card["chat_title"] = ""
    return cards


def waiting_list() -> Dict[str, Any]:
    home = core._calls_home()
    ages = {"approval": watch_local_live._hermes_approval_timeout() + WAITING_GRACE_S}
    clarify = _clarify_timeout()
    if clarify is not None:
        ages["question"] = clarify + WAITING_GRACE_S
    store = hermes_calls._calls_store().CallStore(home)
    listed: Dict[str, Any] = {"waiting": store.waiting_list(max_age=ages, leave_out=_workstream_sessions(home))}
    questions = _open_questions(home)
    if questions:
        listed["questions"] = questions
    # The relay gateway this profile pairs through: a question's card drawn
    # from its row answers through the relay, sealed for this gateway's key
    # on an end-to-end encrypted pairing, and the card came from no push
    # that would have named it (hermes-conduit#2683).
    main = core._load_pairing_state(core._pairing_state_path()) or {}
    gateway_id = main.get("gateway_id")
    if isinstance(gateway_id, str) and gateway_id.strip():
        listed["gateway_id"] = gateway_id.strip()
        # Each device the profile is paired with answers through its own
        # pairing's gateway (hermes-conduit#2758): the main one's above, every
        # device's here by installation id.
        gateways = {}
        for state in (main, *(other for _key, other in core._device_pairings(main))):
            key, gateway = core._installation_key(state.get("installation_id")), state.get("gateway_id")
            if key and isinstance(gateway, str) and gateway.strip():
                gateways[key] = gateway.strip()
        if gateways:
            listed["gateways"] = gateways
    return listed


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
