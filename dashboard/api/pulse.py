"""Pulse settings."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any, Dict

from fastapi import HTTPException, Request, Response

from . import core

router = core.router


# --- Pulse settings ------------------------------------------------------------
#
# Conduit's Settings > Pulse page (TestFlight builds): Pulse's state and the
# settings a tester changes, as pulse_control.py has them. Pulse is one per
# host, in the dashboard's own Hermes home, so these routes take no profile.

# Room for the note to Pulse beside the other settings: pulse_control.NOTE_LIMIT
# characters, each up to twelve bytes once a client JSON-escapes it (a
# surrogate pair for one outside the BMP).
PULSE_MAX_BODY_BYTES = 32 * 1024
PULSE_LIMIT = 60
PULSE_WINDOW_S = 60.0

_pulse_limiter = core._MintLimiter(PULSE_LIMIT, PULSE_WINDOW_S, message="Too many Pulse requests; try again shortly")


def _pulse_call(action: Any, reads_only: bool = False) -> Any:
    """``reads_only``: an action that writes nothing on a host that never
    turned Pulse on, answered there from a scratch store."""
    control = core._plugin_package_module("pulse_control")
    pulse = core._plugin_package_module("pulse")
    home = core._calls_home()
    try:
        if reads_only and not (Path(home) / pulse.store.DB_NAME).exists():
            # Never turned on: answer from the defaults and leave no file behind.
            with tempfile.TemporaryDirectory() as scratch:
                return action(control, pulse.open_store(Path(scratch)), home)
        return action(control, pulse.open_store(home), home)
    except control.Refused as refused:
        # 409 with the reason's code, so Conduit words it; "invalid" is the caller's mistake.
        raise HTTPException(status_code=400 if refused.code == "invalid" else 409,
                            detail={"code": refused.code, **refused.data},
                            headers={"Cache-Control": "no-store"})


async def _pulse_route(name: str, action: Any, reads_only: bool = False) -> Dict[str, Any]:
    try:
        _pulse_limiter.acquire("")
        return {"ok": True, **(await asyncio.to_thread(_pulse_call, action, reads_only))}
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise core._unexpected(name, exc, feature="Pulse")


@router.get("/pulse")
async def get_pulse(response: Response) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return await _pulse_route("status", lambda control, store, home: control.state(store, home), reads_only=True)


@router.put("/pulse")
async def put_pulse(request: Request, response: Response) -> Dict[str, Any]:
    """Changes any of pulse_control.SETTINGS; answers with the new state."""
    response.headers["Cache-Control"] = "no-store"
    body = await core._read_json_body(request, PULSE_MAX_BODY_BYTES)

    def action(control: Any, store: Any, home: Any) -> Dict[str, Any]:
        control.update(store, body, home)
        return control.state(store, home)

    return await _pulse_route("settings", action)


@router.post("/pulse/report")
async def post_pulse_report(response: Response) -> Dict[str, Any]:
    """Asks for a report now."""
    response.headers["Cache-Control"] = "no-store"
    # Refused while Pulse is off, so a host that never turned it on keeps no file.
    return await _pulse_route("report", lambda control, store, home: (control.request_report(store), {})[1],
                              reads_only=True)


@router.post("/pulse/backfill")
async def post_pulse_backfill(request: Request, response: Response) -> Dict[str, Any]:
    """{"action": "estimate"|"start"|"stop"|"cancel", "days": 1-365}:
    an estimate answers {"estimate": {...}}, the others the new state."""
    response.headers["Cache-Control"] = "no-store"
    body = await core._read_json_body(request, PULSE_MAX_BODY_BYTES)
    action_name = body.get("action") if isinstance(body, dict) else None
    days = body.get("days") if isinstance(body, dict) else None
    if action_name not in ("estimate", "start", "stop", "cancel") \
            or (days is not None and (isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 365)):
        raise HTTPException(status_code=400, detail={"code": "invalid", "field": "action" if action_name not in (
            "estimate", "start", "stop", "cancel") else "days"}, headers={"Cache-Control": "no-store"})

    def action(control: Any, store: Any, home: Any) -> Dict[str, Any]:
        if action_name == "estimate":
            return {"estimate": control.backfill_estimate(store, home, days)}
        if action_name == "start":
            control.backfill_start(store, home, days)
        elif action_name == "stop":
            control.backfill_stop(store)
        else:
            control.backfill_cancel(store)
        return control.state(store, home)

    # Only a start can change a host that never turned Pulse on.
    return await _pulse_route("backfill", action, reads_only=action_name != "start")


@router.get("/pulse/memory")
async def get_pulse_memory(response: Response, kind: str = "") -> Dict[str, Any]:
    """What Pulse remembers: open items, most recently mentioned first."""
    response.headers["Cache-Control"] = "no-store"
    return await _pulse_route("memory", lambda control, store, home: {"items": control.items(store, kind, home)},
                              reads_only=True)


@router.get("/pulse/followups")
async def get_pulse_followups(response: Response) -> Dict[str, Any]:
    """Promises Pulse checks on and its last decision for each, newest first."""
    response.headers["Cache-Control"] = "no-store"
    return await _pulse_route("followups", lambda control, store, home: {"followups": control.followups(store)},
                              reads_only=True)


@router.post("/pulse/memory/forget")
async def post_pulse_forget(request: Request, response: Response) -> Dict[str, Any]:
    """{"id": n}: closes one remembered item; {"forgotten": false} if it wasn't open."""
    response.headers["Cache-Control"] = "no-store"
    body = await core._read_json_body(request, PULSE_MAX_BODY_BYTES)
    item_id = body.get("id") if isinstance(body, dict) else None
    return await _pulse_route("forget", lambda control, store, home: {"forgotten": control.forget(store, item_id)},
                              reads_only=True)


@router.get("/pulse/feed")
async def get_pulse_feed(response: Response, proposals: bool = False) -> Dict[str, Any]:
    """The Pulse screen: cards newest first, how many are unseen, each kind's loudness and the Pulse chat's id.
    ``proposals``: the app shows suggestion cards (kind "proposal", each with its "proposal"); without it,
    they're left out and not counted. A card from a Kanban task's worker has "kanban_task", the task's id."""
    response.headers["Cache-Control"] = "no-store"
    return await _pulse_route("feed", lambda control, store, home: control.feed(store, home, proposals=proposals),
                              reads_only=True)


@router.post("/pulse/proposals/{proposal_id}/add")
async def post_pulse_proposal_add(proposal_id: int, response: Response) -> Dict[str, Any]:
    """Add on a suggestion's card: approves it once and asks the phone for the change, which always asks
    there: {"proposal": {...}}, with the change's "change_id", which the app that tapped Add applies without
    the phone's own card. 409 with "already_added" for a second tap, "dismissed", "expired", "not_found", or
    the phone store's code and its "note" (the suggestion stays, to tap again)."""
    response.headers["Cache-Control"] = "no-store"
    return await _pulse_route("proposal_add", lambda control, store, home: {
        "proposal": control.add_proposal(store, proposal_id, home)}, reads_only=True)


@router.post("/pulse/proposals/{proposal_id}/dismiss")
async def post_pulse_proposal_dismiss(proposal_id: int, response: Response) -> Dict[str, Any]:
    """Not now on a suggestion's card: it won't be added, and its card goes: {"proposal": {...}}. 409 with
    "already_added" once Add was tapped, "dismissed" for a second tap, "expired" or "not_found"."""
    response.headers["Cache-Control"] = "no-store"
    return await _pulse_route("proposal_dismiss", lambda control, store, home: {
        "proposal": control.dismiss_proposal(store, proposal_id, home)}, reads_only=True)


@router.post("/pulse/proposals/{proposal_id}/undo")
async def post_pulse_proposal_undo(proposal_id: int, response: Response) -> Dict[str, Any]:
    """Undo on an added suggestion's card: asks the phone to delete it, once, which the app that tapped Undo
    applies without the phone's own card (another device asks first): {"proposal": {...}} with the delete's
    "undo_id". 409 with "not_added" until the phone says it added it, "already_undone" while a delete is on its
    way or done, or the phone store's code and its "note" ("unknown_event" or "unknown_reminder" until a sync has
    sent what it added)."""
    response.headers["Cache-Control"] = "no-store"
    return await _pulse_route("proposal_undo", lambda control, store, home: {
        "proposal": control.undo_proposal(store, proposal_id, home)}, reads_only=True)


@router.post("/pulse/feed/seen")
async def post_pulse_seen(request: Request, response: Response) -> Dict[str, Any]:
    """{"ids": [n, ...]} marks those cards seen, {"ids": null} every card the app's feed shows, so suggestion
    cards only with "proposals": true, as /pulse/feed?proposals=1: {"seen": count}."""
    response.headers["Cache-Control"] = "no-store"
    body = await core._read_json_body(request, PULSE_MAX_BODY_BYTES)
    if not isinstance(body, dict) or "ids" not in body:
        raise HTTPException(status_code=400, detail={"code": "invalid", "field": "ids"},
                            headers={"Cache-Control": "no-store"})
    ids, proposals = body["ids"], body.get("proposals", False)
    return await _pulse_route("seen", lambda control, store, home: {
        "seen": control.see(store, ids, proposals=proposals)}, reads_only=True)


@router.post("/pulse/rate")
async def post_pulse_rate(request: Request, response: Response) -> Dict[str, Any]:
    """{"card": n, "rating": "up"|"down", "reason": optional, for a down:
    "not_relevant"|"already_knew"|"too_late"|"too_often"|"wrong_time"}:
    {"learned": [...], "rest_until": epoch seconds or null}, the lessons it
    made, and for "wrong_time" when the card, swiped away, comes back. A
    report item's card ("noticed") takes a reason too, for its topic
    (pulse_learning.lesson_kind); "not_relevant" makes a rule only for one
    from a chat."""
    response.headers["Cache-Control"] = "no-store"
    body = await core._read_json_body(request, PULSE_MAX_BODY_BYTES)
    body = body if isinstance(body, dict) else {}

    def action(control: Any, store: Any, home: Any) -> Dict[str, Any]:
        reason = body.get("reason") or ""
        learned = control.rate(store, body.get("card"), body.get("rating"), reason, home=home)
        card = store.card(body["card"]) if reason == "wrong_time" else None  # a card that isn't there was refused
        return {"learned": learned, "rest_until": card.get("rest_until") if card else None}

    return await _pulse_route("rate", action, reads_only=True)


@router.post("/pulse/done")
async def post_pulse_done(request: Request, response: Response) -> Dict[str, Any]:
    """{"card": n, "done": true|false}: marks a follow-up or report item
    card done (a promise behind it closes, no rating) or takes the mark off:
    {"changed": bool}."""
    response.headers["Cache-Control"] = "no-store"
    body = await core._read_json_body(request, PULSE_MAX_BODY_BYTES)
    body = body if isinstance(body, dict) else {}
    # "done" is required: a body without it changes nothing.
    return await _pulse_route("done", lambda control, store, home: {"changed": control.done(
        store, body.get("card"), body.get("done"))}, reads_only=True)


@router.post("/pulse/dismiss")
async def post_pulse_dismiss(request: Request, response: Response) -> Dict[str, Any]:
    """{"card": n, "dismissed": true|false, "until": "later_today"|"tomorrow"|"next_week"}:
    swipes a card away ("Not now": off the list, nothing learned) until
    then, when it comes back as new, or for good without "until"; or puts
    it back: {"changed": bool, "rest_until": epoch seconds or null}."""
    response.headers["Cache-Control"] = "no-store"
    body = await core._read_json_body(request, PULSE_MAX_BODY_BYTES)
    body = body if isinstance(body, dict) else {}

    def action(control: Any, store: Any, home: Any) -> Dict[str, Any]:
        # "dismissed" is required: a body without it changes nothing.
        changed = control.dismiss(store, body.get("card"), body.get("dismissed"), until=body.get("until"), home=home)
        card = store.card(body["card"])  # a card that isn't there was refused above
        return {"changed": changed, "rest_until": card.get("rest_until") if card else None}

    return await _pulse_route("dismiss", action, reads_only=True)


@router.post("/pulse/acted")
async def post_pulse_acted(request: Request, response: Response) -> Dict[str, Any]:
    """{"card": n}: Do it on a follow-up or report item card. Pulse reads
    the chat Do it opened once it has answered (for a report item from no
    chat or a card from a Kanban task's worker, the other chats on its
    subject) and marks the card done if it got
    done: {"booked": bool}."""
    response.headers["Cache-Control"] = "no-store"
    body = await core._read_json_body(request, PULSE_MAX_BODY_BYTES)
    body = body if isinstance(body, dict) else {}
    return await _pulse_route("acted", lambda control, store, home: {"booked": control.acted(
        store, body.get("card"))}, reads_only=True)


@router.get("/pulse/learned")
async def get_pulse_learned(response: Response) -> Dict[str, Any]:
    """What ratings taught Pulse, newest first, and each kind's loudness."""
    response.headers["Cache-Control"] = "no-store"
    return await _pulse_route("learned", lambda control, store, home: control.learned(store, home), reads_only=True)


@router.post("/pulse/learned/undo")
async def post_pulse_undo(request: Request, response: Response) -> Dict[str, Any]:
    """{"id": n}: undoes one lesson; answers with what's learned now."""
    response.headers["Cache-Control"] = "no-store"
    body = await core._read_json_body(request, PULSE_MAX_BODY_BYTES)
    lesson_id = body.get("id") if isinstance(body, dict) else None

    def action(control: Any, store: Any, home: Any) -> Dict[str, Any]:
        return {"undone": control.undo(store, lesson_id), **control.learned(store, home)}

    return await _pulse_route("undo", action, reads_only=True)


@router.post("/pulse/learned/reset")
async def post_pulse_reset(request: Request, response: Response) -> Dict[str, Any]:
    """{"scope": "kind"|"place"|"timely"|"loud"|"everything", "value": "..."}:
    forgets what learning holds about one thing ("value" as /pulse/learned's
    preferences give it, "" for everything); answers with the lesson, which
    /pulse/learned/undo takes back, and what's learned now."""
    response.headers["Cache-Control"] = "no-store"
    body = await core._read_json_body(request, PULSE_MAX_BODY_BYTES)
    body = body if isinstance(body, dict) else {}

    def action(control: Any, store: Any, home: Any) -> Dict[str, Any]:
        lesson = control.reset(store, body.get("scope"), body.get("value", ""), home=home)
        return {"reset": lesson, **control.learned(store, home)}

    return await _pulse_route("reset", action, reads_only=True)


@router.get("/pulse/left_out")
async def get_pulse_left_out(response: Response) -> Dict[str, Any]:
    """What the latest reports and look aheads considered and left out, and
    why: those learning or the user's rules kept out first. Each entry's
    "from" says which ("report" or "look_ahead")."""
    response.headers["Cache-Control"] = "no-store"
    return await _pulse_route("left_out", lambda control, store, home: {"left_out": control.left_out(store, home)},
                              reads_only=True)


@router.post("/pulse/left_out/bring_up")
async def post_pulse_bring_up(request: Request, response: Response) -> Dict[str, Any]:
    """{"id": n}: makes a card on the Pulse screen of something a report
    left out (its id in /pulse/left_out): {"card": {...}}, as the feed lists
    it, or the card it already became."""
    response.headers["Cache-Control"] = "no-store"
    body = await core._read_json_body(request, PULSE_MAX_BODY_BYTES)
    candidate_id = body.get("id") if isinstance(body, dict) else None
    return await _pulse_route("bring_up", lambda control, store, home: {"card": control.bring_up(
        store, candidate_id, home)}, reads_only=True)


@router.put("/pulse/levels")
async def put_pulse_levels(request: Request, response: Response) -> Dict[str, Any]:
    """{"kind": "followup", "level": "push"|"screen"|"brief"|"off"}: sets a
    kind's loudness by hand; answers with what's learned now."""
    response.headers["Cache-Control"] = "no-store"
    body = await core._read_json_body(request, PULSE_MAX_BODY_BYTES)
    body = body if isinstance(body, dict) else {}

    def action(control: Any, store: Any, home: Any) -> Dict[str, Any]:
        control.set_level(store, body.get("kind"), body.get("level"))
        return control.learned(store)

    # Refused while Pulse is off, so a host that never turned it on keeps no file.
    return await _pulse_route("levels", action, reads_only=True)
