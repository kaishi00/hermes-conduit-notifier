"""Linked Rooms: asks between linked agents (link_asks.py), the rules each
Link's asks are answered by and the asks waiting for their owner.
"""

from __future__ import annotations

from typing import Any, Callable, Dict

from fastapi import HTTPException, Request, Response

from . import core, linked_rooms, phone_data

logger = core.logger
router = core.router

LINK_ASKS_MAX_BODY_BYTES = 4 * 1024


def link_asks(action: Callable[[Any], Dict[str, Any]]) -> Dict[str, Any]:
    module = core._plugin_package_module("link_asks")
    root = linked_rooms._room_links_root()
    linked_rooms._start_room_handlers(root)
    try:
        return action(module.Asks(root))
    except module.AsksError as exc:
        raise core.TokenError(exc.status, str(exc), exc.code)


async def _link_asks_route(action: Callable[[Any], Dict[str, Any]], route: str) -> Any:
    try:
        answer = await core._run_scoped(None, core._limited(linked_rooms._room_links_limiter, None,
                                                            lambda: link_asks(action)))
        return {"ok": True, **answer}
    except core.TokenError as exc:
        return phone_data._store_refusal(exc)
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise core._unexpected(route, exc, feature="Linked Rooms")


@router.get("/links/rules")
async def get_link_rules(response: Response) -> Any:
    """Each Link's rules for its asks (the defaults where none were set):
    whether they're free on a date (yes_no, times, ask or off), a question
    (ask or off), and the profile whose calendar and phone answer."""
    response.headers["Cache-Control"] = "no-store"
    return await _link_asks_route(lambda asks: asks.rules(), "link rules")


@router.put("/links/rules")
async def put_link_rules(request: Request, response: Response) -> Any:
    """Changes one Link's rules: ``link_id`` and what changes."""
    response.headers["Cache-Control"] = "no-store"
    body = await core._read_json_body(request, LINK_ASKS_MAX_BODY_BYTES)
    if not isinstance(body, dict) or not isinstance(body.get("link_id"), str):
        raise HTTPException(status_code=400, headers={"Cache-Control": "no-store"},
                            detail='Expected {"link_id": ..., "free_on" | "questions" | "profile": ...}')
    changes = {key: value for key, value in body.items() if key != "link_id"}
    return await _link_asks_route(lambda asks: asks.set_rules(body["link_id"], changes), "link rules")


@router.get("/links/asks")
async def get_link_asks(response: Response) -> Any:
    """The asks linked Hermes made of this one, newest first, each with how it
    went; those still waiting for the owner have ``status`` held."""
    response.headers["Cache-Control"] = "no-store"
    return await _link_asks_route(lambda asks: {"asks": asks.received()}, "link asks")
