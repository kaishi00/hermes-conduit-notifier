"""Typed phone consent: interactive owner authentication AND enrolled-key proof."""
from __future__ import annotations

from typing import Optional

from fastapi import HTTPException, Request, Response

from . import core, workstreams

router = core.router


def _invoke(request, profile, operation, body=None):
    actor = workstreams._grant_actor(request, profile)
    tool = workstreams._work_tool()
    consent = tool.phone_consent
    try:
        if body is None:
            return consent.preview(core._calls_home(), tool, actor)
        return getattr(consent, operation)(core._calls_home(), tool, actor, body)
    except consent.Refusal as exc:
        raise core.TokenError(exc.status, str(exc))


@router.get("/phone-consent")
async def get_phone_consent(request: Request, response: Response, profile: Optional[str] = None):
    response.headers["Cache-Control"] = "no-store"
    profile = await core._conduit_route_profile_no_store(request, profile)
    return await workstreams._workstreams_route("phone consent preview", profile,
                                                lambda: _invoke(request, profile, "preview"))


async def _write(request, response, profile, operation):
    response.headers["Cache-Control"] = "no-store"
    profile = await core._conduit_route_profile_no_store(request, profile)
    body = await core._read_json_body(request, 4096)
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected a JSON object", headers={"Cache-Control": "no-store"})
    return await workstreams._workstreams_route("phone consent " + operation, profile,
        lambda: _invoke(request, profile, operation, body), workstreams._workstream_actions_limiter)


@router.post("/phone-consent/challenge")
async def post_phone_challenge(request: Request, response: Response, profile: Optional[str] = None):
    return await _write(request, response, profile, "challenge")


@router.post("/phone-consent/receipt")
async def post_phone_receipt(request: Request, response: Response, profile: Optional[str] = None):
    return await _write(request, response, profile, "receipt")


@router.post("/phone-consent/revoke-device")
async def post_phone_revoke(request: Request, response: Response, profile: Optional[str] = None):
    # Owner-only recovery also works after a phone/key is lost. It cannot enroll.
    return await _write(request, response, profile, "revoke")
