"""What Conduit shares: health data and the voice tools that read it, location,
the Shortcuts inbox, calendar, reminders, Focus and phone status.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import HTTPException, Request, Response

from . import core

logger = core.logger
router = core.router


# --- Health data (Apple Health shared from Conduit) ---------------------------
#
# The user turns on health sharing in Conduit, per category. The phone agrees
# a key with this profile once (/health/key, X25519: only public keys cross
# the wire) and then posts sealed batches of HealthKit totals and events
# (/health/ingest) straight here, never through the push relay. The store
# (health_store.py) keeps them in the profile's conduit-health.sqlite3 for the
# conduit_health tool. Status reports counts and dates only and wipe
# deletes; the one route that returns health data is the sealed voice read
# below, which answers only the device that shared it.

HEALTH_KEY_LIMIT = 10
HEALTH_KEY_WINDOW_S = 60.0
HEALTH_WIPE_LIMIT = 5
HEALTH_WIPE_WINDOW_S = 60.0
# A backfill sends many batches in a row.
HEALTH_INGEST_LIMIT = 120
HEALTH_INGEST_WINDOW_S = 60.0
HEALTH_MAX_KEY_BODY_BYTES = 4 * 1024
# A sealed batch is base64url of at most ~3 MiB of JSON.
HEALTH_MAX_INGEST_BODY_BYTES = 4 * 1024 * 1024

_health_key_limiter = core._MintLimiter(HEALTH_KEY_LIMIT, HEALTH_KEY_WINDOW_S,
                                   message="Too many health key requests; try again shortly")
_health_ingest_limiter = core._MintLimiter(HEALTH_INGEST_LIMIT, HEALTH_INGEST_WINDOW_S,
                                      message="Too many health uploads; try again shortly")
_health_wipe_limiter = core._MintLimiter(HEALTH_WIPE_LIMIT, HEALTH_WIPE_WINDOW_S,
                                    message="Too many health delete requests; try again shortly")
_health_store_module: Any = None


def _health_store() -> Any:
    """health_store.py in the plugin's folder, loaded once."""
    global _health_store_module
    if _health_store_module is None:
        _health_store_module = core._plugin_package_module("health_store")
    return _health_store_module


def _health_call(fn: Callable[[Any], Dict[str, Any]]) -> Dict[str, Any]:
    module = _health_store()
    try:
        return fn(module.HealthStore(core._calls_home()))
    except module.HealthError as exc:
        raise core.TokenError(exc.status, str(exc), exc.code)


async def _health_route(response: Response, profile: Optional[str], fn: Callable[[], Dict[str, Any]],
                        name: str) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        return {"ok": True, **(await core._run_scoped(profile, fn))}
    except core.TokenError as exc:
        return _store_refusal(exc)
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise core._unexpected(name, exc, feature="Health sync")


def _store_refusal(exc: core.TokenError) -> Any:
    """A health or location store's refusal: ``detail`` is the message as
    always, and ``code`` the store's word for it (``unknown_key``,
    ``bad_seal``, ``too_many_devices``, ``schema``…), so Conduit can tell a
    lost key from a full host without reading the message."""
    if not exc.code:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    from fastapi.responses import JSONResponse

    return JSONResponse({"detail": str(exc), "code": exc.code}, status_code=exc.status,
                        headers={"Cache-Control": "no-store"})


@router.get("/health/status")
async def get_health_status(response: Response, profile: Optional[str] = None,
                            device_id: Optional[str] = None) -> Dict[str, Any]:
    return await _health_route(response, profile, lambda: _health_call(lambda store: {
        **store.status(device_id), "crypto": _health_store().crypto_available()}), "status")


@router.post("/health/key")
async def post_health_key(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    await core._scoped_acquire(_health_key_limiter, profile)
    body = await core._read_json_body(request, HEALTH_MAX_KEY_BODY_BYTES)
    return await _health_route(response, profile, lambda: _health_call(lambda store: store.register_key(body)), "key")


@router.post("/health/ingest")
async def post_health_ingest(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    await core._scoped_acquire(_health_ingest_limiter, profile)
    body = await core._read_json_body(request, HEALTH_MAX_INGEST_BODY_BYTES)
    return await _health_route(response, profile, lambda: _health_call(lambda store: store.ingest(body)), "upload")


@router.post("/health/wipe")
async def post_health_wipe(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    await core._scoped_acquire(_health_wipe_limiter, profile)
    body = await core._read_json_body(request, HEALTH_MAX_KEY_BODY_BYTES)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object", headers={"Cache-Control": "no-store"})
    # Everything only when asked for by name; a device id alone deletes that device's data.
    if body.get("all") is True and body.get("device_id") is not None:
        raise HTTPException(status_code=400, detail="Name a device_id or all: true, not both",
                            headers={"Cache-Control": "no-store"})
    device_id = None if body.get("all") is True else body.get("device_id")
    if device_id is None and body.get("all") is not True:
        raise HTTPException(status_code=400, detail="Name a device_id, or all: true",
                            headers={"Cache-Control": "no-store"})
    return await _health_route(response, profile, lambda: _health_call(lambda store: store.wipe(device_id)), "delete")


# --- Voice tools (a live voice call reads the user's shared data) ------------
#
# Gemini Live, Grok Live and GPT-Live run at Google, xAI and OpenAI, driven by
# Conduit on the phone, so they can't call Hermes' own tools. With the user's
# say-so (a switch per voice mode in Conduit's Settings > Health or Settings >
# Location, off by default, naming the voice provider), Conduit offers them
# read-only versions of conduit_health and conduit_location here:
# GET /voice-tools lists what this profile can answer, with the description
# and parameters Hermes' own tool has, and POST /voice-tools/call runs the
# same handler.
#
# This is the one route that returns health or location data, so it answers
# only the device that shared it: the request and the answer are sealed with
# that device's own health or location key (its own HKDF directions, separate
# from ingest), so dashboard access alone opens nothing, and the read covers
# only the data that device uploaded, matched by id. Read actions only;
# Conduit keeps the log of what each call read (the host also logs location
# reads, as every location read is). That switch is Conduit's: the host can't
# see it, and the seal is the host's boundary (only the device can ask, and
# only about itself). (hermes-conduit designs/voice-health-location.md)

VOICE_TOOLS_LIMIT = 60
VOICE_TOOLS_WINDOW_S = 60.0
VOICE_TOOLS_MAX_BODY_BYTES = 8 * 1024
VOICE_TOOLS_TIMEOUT_S = 10.0
# A read is a few indexed SQLite queries; a timed-out one keeps its worker
# until it finishes (threads can't be cancelled), which the 60/min cap and
# SQLite's 15 s busy timeout bound.
VOICE_TOOLS_WORKERS = 2

_voice_tools_limiter = core._MintLimiter(VOICE_TOOLS_LIMIT, VOICE_TOOLS_WINDOW_S,
                                    message="Too many voice reads; try again shortly")
_voice_tools_executor = ThreadPoolExecutor(max_workers=VOICE_TOOLS_WORKERS, thread_name_prefix="conduit-voice-tools")


_VOICE_PROVIDER = re.compile(r"[a-z0-9-]{1,40}")


def _voice_tool_modules() -> Dict[str, Tuple[Any, Any, Any, str]]:
    """Voice-readable tools by name: (tool module, store class, its error,
    sealed key). These modules are package imports, so their store module is
    a second copy beside the path-loaded one the location and health routes
    use, with its own thread lock; writes from both stay safe through
    SQLite's own locking (WAL, BEGIN IMMEDIATE, the busy timeout)."""
    modules: Dict[str, Tuple[Any, Any, Any, str]] = {}
    for name, store, error, key in (("health_tool", "HealthStore", "HealthError", "health"),
                                    ("location_tool", "LocationStore", "LocationError", "location")):
        # One tool missing or broken in a partial install doesn't take the
        # other down.
        try:
            tool = core._plugin_package_module(name)
            modules[tool.TOOL_NAME] = (tool, getattr(tool, store), getattr(tool, error), key)
        except Exception:
            logger.warning("Conduit voice tools: %s unavailable", name, exc_info=True)
    return modules


def _voice_tool_entries(devices: Optional[Dict[str, Optional[str]]] = None) -> Dict[str, Dict[str, Any]]:
    """Voice-readable tools by name: their schema, and which sealed key they
    use. ``devices`` names the calling device per sealed key ("health",
    "location", "phone"): a tool is then offered as that device shares it,
    since its reads see that device alone; without one, as any device does."""
    devices = devices or {}
    entries: Dict[str, Dict[str, Any]] = {}
    for name, (tool, store_class, _, key) in _voice_tool_modules().items():
        # A store that can't be read (damaged file) hides that tool alone.
        try:
            store = store_class(core._calls_home())
            device_id = (devices.get(key) or "").strip()
            if not (tool.voice_shared(store, device_id) if device_id else tool.voice_shared(store)):
                continue
            entries[name] = {
                "key": key,
                "description": tool.voice_description(),
                "parameters": tool.voice_parameters(),
            }
        except Exception:
            logger.warning("Conduit voice tools: %s store unreadable", name, exc_info=True)
    # The calendar and phone status, sealed with the phone feature's own key.
    # A problem there takes only those tools off the list.
    try:
        phone = core._plugin_package_module("phone_tool")
        offered = phone.voice_tools(phone.PhoneStore(core._calls_home()), (devices.get("phone") or "").strip() or None)
    except Exception:  # noqa: BLE001
        logger.warning("Conduit voice tools: the calendar and phone tools couldn't be listed", exc_info=True)
        offered = {}
    for name, entry in offered.items():
        entries[name] = {"key": "phone", **entry}
    return entries


def voice_tools_list(devices: Optional[Dict[str, Optional[str]]] = None) -> Dict[str, Any]:
    return {"tools": [{"name": name, **entry} for name, entry in _voice_tool_entries(devices).items()]}


def voice_tools_call(envelope: Any) -> Dict[str, Any]:
    # Routed on the unsealed op: each branch opens its own seal, whose AAD
    # binds the op, so a misrouted envelope fails there.
    if isinstance(envelope, dict) and envelope.get("op") == "voice":
        return _phone_voice_call(envelope)
    modules = _voice_tool_modules()
    name = envelope.get("tool") if isinstance(envelope, dict) else None
    if not isinstance(name, str) or name not in modules:
        raise core.TokenError(404, "This host has no voice tool by that name")
    tool, store_class, error_class, _ = modules[name]
    store = store_class(core._calls_home())
    try:
        device_id, request, seal_answer = store.open_voice_request(envelope)
    except error_class as exc:
        raise core.TokenError(exc.status, str(exc), exc.code) from exc
    provider = request.get("provider")
    provider = provider if isinstance(provider, str) and _VOICE_PROVIDER.fullmatch(provider) else "voice"
    args = request.get("args")
    action = args.get("action") if isinstance(args, dict) else None
    logger.info("Conduit voice read: %s %s for %s", tool.TOOL_NAME,
                action if isinstance(action, str) and action in tool.VOICE_ACTIONS else "other", provider)
    answer = tool.voice_handle(args, device_id=device_id, store=store, provider=provider)
    try:
        # Refused if the device registered a new key during the read.
        return {"v": 1, **seal_answer(answer)}
    except error_class as exc:
        raise core.TokenError(exc.status, str(exc), exc.code) from exc


def _phone_voice_call(envelope: Dict[str, Any]) -> Dict[str, Any]:
    """A calendar, phone status, alarms or contacts read, sealed with the
    device's phone exchange keys (op "voice"); the tool's name is inside the
    seal."""
    phone = core._plugin_package_module("phone_tool")
    store = phone.PhoneStore(core._calls_home())
    try:
        device_id, request, seal_answer = store.open_voice_request(envelope)
    except phone.PhoneError as exc:
        raise core.TokenError(exc.status, str(exc), exc.code) from exc
    name = request.get("tool")
    if name not in phone.VOICE_TOOLS:
        raise core.TokenError(404, "This host has no voice tool by that name")
    provider = request.get("provider")
    args = request.get("args")
    action = args.get("action") if isinstance(args, dict) else None
    logger.info("Conduit voice read: %s %s for %s", name,
                action if isinstance(action, str)
                and action in phone.VOICE_CALENDAR_ACTIONS + phone.VOICE_PHONE_ACTIONS + ("people",) else "other",
                provider if isinstance(provider, str) and _VOICE_PROVIDER.fullmatch(provider) else "voice")
    answer = phone.voice_handle(name, args, device_id=device_id, store=store)
    try:
        # Refused if the device registered a new key during the read.
        return {"v": 1, **seal_answer({"result": answer})}
    except phone.PhoneError as exc:
        raise core.TokenError(exc.status, str(exc), exc.code) from exc


async def _voice_tools_route(response: Response, profile: Optional[str], fn: Callable[[], Dict[str, Any]],
                             name: str) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        return {"ok": True, **(await asyncio.wait_for(core._run_scoped(profile, fn, _voice_tools_executor),
                                                       timeout=VOICE_TOOLS_TIMEOUT_S))}
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail=f"Voice tools {name} timed out",
                            headers={"Cache-Control": "no-store"})
    except core.TokenError as exc:
        return _store_refusal(exc)
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise core._unexpected(name, exc, feature="Voice tools")


@router.get("/voice-tools")
async def get_voice_tools(response: Response, profile: Optional[str] = None, health_device: Optional[str] = None,
                          location_device: Optional[str] = None,
                          phone_device: Optional[str] = None) -> Dict[str, Any]:
    """The tools a call may use. Conduit names its own device in each store
    (``*_device``), so it's offered only what that device's reads can answer."""
    response.headers["Cache-Control"] = "no-store"
    await core._scoped_acquire(_voice_tools_limiter, profile)
    devices = {"health": health_device, "location": location_device, "phone": phone_device}
    return await _voice_tools_route(response, profile, lambda: voice_tools_list(devices), "list")


@router.post("/voice-tools/call")
async def post_voice_tools_call(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    await core._scoped_acquire(_voice_tools_limiter, profile)
    body = await core._read_json_body(request, VOICE_TOOLS_MAX_BODY_BYTES)
    return await _voice_tools_route(response, profile, lambda: voice_tools_call(body), "call")


# --- Location (shared from Conduit) ---------------------------------------
#
# The user turns on location sharing in Conduit: while they talk to Hermes,
# and, separately, in the background. Like health, the phone agrees a key
# with this profile once (/location/key) and then posts sealed batches
# (/location/ingest) straight here, never through the push relay. The store
# (location_store.py) keeps the latest fix, arrivals and departures for the
# user's chosen history, place rules and the agent's read log. No route
# returns a location: status and the read log say when and how much, rules
# are the user's own, and wipe deletes. An arrival or departure that matches
# a place rule starts that rule's scheduled job (Hermes's cron.jobs, which
# runs it on the gateway's next tick).

LOCATION_KEY_LIMIT = 10
LOCATION_KEY_WINDOW_S = 60.0
LOCATION_INGEST_LIMIT = 60
LOCATION_INGEST_WINDOW_S = 60.0
LOCATION_WIPE_LIMIT = 5
LOCATION_WIPE_WINDOW_S = 60.0
LOCATION_MAX_KEY_BODY_BYTES = 4 * 1024
LOCATION_MAX_INGEST_BODY_BYTES = 256 * 1024

_location_key_limiter = core._MintLimiter(LOCATION_KEY_LIMIT, LOCATION_KEY_WINDOW_S,
                                     message="Too many location key requests; try again shortly")
_location_ingest_limiter = core._MintLimiter(LOCATION_INGEST_LIMIT, LOCATION_INGEST_WINDOW_S,
                                        message="Too many location uploads; try again shortly")
_location_wipe_limiter = core._MintLimiter(LOCATION_WIPE_LIMIT, LOCATION_WIPE_WINDOW_S,
                                      message="Too many location delete requests; try again shortly")
_location_store_module: Any = None


def _location_store() -> Any:
    """location_store.py in the plugin's folder, loaded once (with sealing.py, as
    part of the private package)."""
    global _location_store_module
    if _location_store_module is None:
        _location_store_module = core._plugin_package_module("location_store")
    return _location_store_module


def _location_call(fn: Callable[[Any], Dict[str, Any]]) -> Dict[str, Any]:
    module = _location_store()
    try:
        return fn(module.LocationStore(core._calls_home()))
    except module.LocationError as exc:
        raise core.TokenError(exc.status, str(exc), exc.code)
    except sqlite3.DatabaseError:
        logger.warning("conduit_push: location store unreadable", exc_info=True)
        raise core.TokenError(503, "The location store on this host can't be read")


async def _location_route(response: Response, profile: Optional[str], fn: Callable[[], Dict[str, Any]],
                          name: str) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        return {"ok": True, **(await core._run_scoped(profile, fn))}
    except core.TokenError as exc:
        return _store_refusal(exc)
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise core._unexpected(name, exc, feature="Location sharing")


def _start_rule_jobs(fired: Any) -> list:
    """Starts the scheduled job of each place rule an ingest set off; runs
    inside the profile's scope. The rule already counts as run today, so a
    job that can't start is logged, not retried."""
    started = []
    for rule in fired if isinstance(fired, list) else []:
        job_id = rule.get("job_id") if isinstance(rule, dict) else None
        if not job_id:
            continue
        try:
            from cron.jobs import trigger_job

            if trigger_job(job_id, extra_prompt=rule.get("prompt")):
                started.append(rule.get("rule_id"))
            else:
                logger.warning("conduit_push: place rule %s has no job %s", rule.get("rule_id"), job_id)
        except Exception:  # noqa: BLE001 — the upload itself succeeded
            logger.warning("conduit_push: could not start place rule %s", rule.get("rule_id"), exc_info=True)
    return started


def _clear_rule_jobs() -> None:
    """Removes the jobs of deleted place rules still on the store's list
    (#227); runs inside the profile's scope. The store listed each one as it
    deleted the rule, so a job that can't go now (or a crash before this) is
    retried on the next rule change here, in the tool, or at the next start.
    A job counts as gone once remove_job removes it, or answers False and
    get_job confirms the scheduler doesn't have it; anything else keeps it
    listed."""
    try:
        store = _location_store().LocationStore(core._calls_home())
        pending = store.pending_rule_jobs()
    except Exception:  # noqa: BLE001 — the change itself succeeded
        logger.warning("conduit_push: place rule jobs not read", exc_info=True)
        return
    gone = []
    for job_id in pending:
        try:
            from cron import jobs as cron_jobs

            if cron_jobs.remove_job(job_id) or cron_jobs.get_job(job_id) is None:
                gone.append(job_id)
            else:
                logger.warning("conduit_push: place rule job %s not removed yet", job_id)
        except Exception:  # noqa: BLE001 — the rule is gone; its job's prompt does nothing without an event
            logger.warning("conduit_push: could not remove place rule job %s", job_id, exc_info=True)
    try:
        store.rule_jobs_gone(gone)
    except Exception:  # noqa: BLE001 — still listed: removed again next time, which is harmless
        logger.warning("conduit_push: removed place rule jobs not taken off the list", exc_info=True)


def _location_ingest(body: Any) -> Dict[str, Any]:
    result = _location_call(lambda store: store.ingest(body, reply=_location_spots))
    fired = result.pop("rules_fired", [])
    result["rules_fired"] = len(_start_rule_jobs(fired))
    _location_tell_pulse(result.pop("moments", []))
    for key in ("spot_entries", "spots_room", "spots_watching", "spots_keep"):
        result.pop(key, None)
    return result


def _location_spots(device_id: str, result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """What goes back, sealed, to a phone that watches shops for Pulse
    (pulse_spots): the shops it said the user walked into become moments
    in the profile whose watch each was, and the reply lists what to watch
    now, every reading profile's. Only where Pulse runs; the batch is
    stored whatever happens here, and a failure sends no list (the phone
    keeps watching what it has until its time runs out). Paused, or sharing
    in the background at less than Precise: an empty list. An older batch
    arriving late (stale) says nothing either way: no list."""
    try:
        spots = core._plugin_package_module("pulse_spots")
        places = core._plugin_package_module("pulse_places")
        pulse_store = core._plugin_package_module("pulse_store")
        if result.get("stale"):
            return None
        if not result.get("spots_watching"):
            return {"spots": []}
        homes = places.homes(Path(core._calls_home()))
        now = time.time()
        if spots.entered(homes, device_id, result.get("spot_entries") or [], now, keep=result.get("spots_keep", True)):
            _wake_running_pulse()
        if result.get("spots_room") is not None:
            for home in homes:
                if (home / pulse_store.DB_NAME).exists():
                    spots.phone_said(pulse_store.open_store(home), result["spots_room"], now)
        return spots.reply(homes, now)
    except Exception:  # noqa: BLE001 — taking the phone's batch never fails on Pulse
        logger.warning("conduit_push: location couldn't tell Pulse about shops walked into", exc_info=True)
        return None


def _location_tell_pulse(moments: Any) -> None:
    """Arrivals and departures at saved places in: Pulse makes due what
    waits for them (pulse_places), with Where you are on, here and in each
    profile that reads this location. Only where Pulse runs; the batch is
    stored whatever happens here."""
    if not moments:
        return
    try:
        pulse_store = core._plugin_package_module("pulse_store")
        places = core._plugin_package_module("pulse_places")
        told = False
        for home in places.homes(Path(core._calls_home())):
            if not (home / pulse_store.DB_NAME).exists():
                continue  # Pulse never turned on there: leave no file behind
            try:
                told = places.arrived(pulse_store.open_store(home), moments, time.time()) or told
            except Exception:  # noqa: BLE001 — the others still hear it
                logger.warning("conduit_push: location couldn't tell a profile's Pulse about arrivals or departures",
                               exc_info=True)
        if told:
            _wake_running_pulse()  # a loop in another profile's process finds them on its next tick
    except Exception:  # noqa: BLE001 — taking the phone's batch never fails on Pulse
        logger.warning("conduit_push: location couldn't tell Pulse about arrivals or departures", exc_info=True)


def _location_wipe(device_id: Any) -> Dict[str, Any]:
    readers = _location_pulse_homes()  # before the wipe ends their sharing
    result = _location_call(lambda store: store.wipe(device_id))
    result.pop("rule_jobs", None)
    _clear_rule_jobs()
    _location_forget_pulse(readers, device_id)
    return result


def _location_pulse_homes() -> List[Path]:
    try:
        return core._plugin_package_module("pulse_places").homes(Path(core._calls_home()))
    except Exception:  # noqa: BLE001 — this home's at least
        logger.warning("conduit_push: location couldn't list the profiles that read it", exc_info=True)
        return [Path(core._calls_home())]


def _location_forget_pulse(homes: List[Path], device_id: Optional[str]) -> None:
    """A wipe deletes the arrivals and departures Pulse kept a day too
    (that device's, or all), wherever this location reached."""
    try:
        pulse_store = core._plugin_package_module("pulse_store")
    except Exception:  # noqa: BLE001 — they go within a day anyway
        logger.warning("conduit_push: location wipe couldn't clear Pulse's moments", exc_info=True)
        return
    for home in homes:
        try:
            if (home / pulse_store.DB_NAME).exists():
                pulse_store.open_store(home).clear_moments(device_id)
        except Exception:  # noqa: BLE001 — they go within a day anyway
            logger.warning("conduit_push: location wipe couldn't clear Pulse's moments", exc_info=True)


def _location_remove_rule(rule_id: str) -> Dict[str, Any]:
    job_id = _location_call(lambda store: {"job": store.remove_rule(rule_id)})["job"]
    if job_id is None:
        raise core.TokenError(404, "No place rule has that id")
    _clear_rule_jobs()
    return {"removed": True}


@router.get("/location/status")
async def get_location_status(response: Response, profile: Optional[str] = None,
                              device_id: Optional[str] = None) -> Dict[str, Any]:
    # `locate` tells Conduit whether conduit_location can ask the phone for a
    # fresh fix: the same pairing gate as the tool's own (not_paired).
    return await _location_route(response, profile, lambda: _location_call(lambda store: {
        **store.status(device_id), "crypto": _location_store().crypto_available(),
        "locate": {"available": core._load_pairing_state(core._pairing_state_path()) is not None}}), "status")


@router.post("/location/key")
async def post_location_key(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    await core._scoped_acquire(_location_key_limiter, profile)
    body = await core._read_json_body(request, LOCATION_MAX_KEY_BODY_BYTES)
    return await _location_route(response, profile,
                                 lambda: _location_call(lambda store: store.register_key(body)), "key")


@router.post("/location/ingest")
async def post_location_ingest(request: Request, response: Response,
                               profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    await core._scoped_acquire(_location_ingest_limiter, profile)
    body = await core._read_json_body(request, LOCATION_MAX_INGEST_BODY_BYTES)
    return await _location_route(response, profile, lambda: _location_ingest(body), "upload")


# --- Shortcuts inbox ---------------------------------------------------------
#
# The user's iPhone Shortcuts automations (a notification from an app they
# pick, a message, an email, a Wallet payment, an NFC tag, a sound) end in
# Conduit's Log to Hermes action, which posts each run here as a sealed event
# (/inbox/ingest) after agreeing a key once (/inbox/key), straight over the
# dashboard connection, never through the push relay. No chat turn and no
# push: the conduit_inbox tool reads them (inbox_store.py,
# conduit-inbox.sqlite3), and Pulse, when it's on, reads them in its report
# and checks the user's watches when new ones arrive. Status reports counts
# only. Conduit's Settings page reads a device's own events back sealed for
# it (/inbox/list), deletes the ones the user picks (/inbox/delete), and
# wipe deletes a device's or everything.

INBOX_KEY_LIMIT = 10
INBOX_KEY_WINDOW_S = 60.0
INBOX_WIPE_LIMIT = 5
INBOX_WIPE_WINDOW_S = 60.0
# A burst of notifications can each run an automation.
INBOX_INGEST_LIMIT = 120
INBOX_INGEST_WINDOW_S = 60.0
INBOX_MAX_KEY_BODY_BYTES = 4 * 1024
# The largest batch the store keeps whole: inbox_store.MAX_EVENTS_PER_BATCH
# events whose fields are at their MAX_*_CHARS (source 120, title 200, text
# 4000, amount 40, trigger 40; Conduit cuts to these), at up to 4 UTF-8
# bytes a character plus id and JSON, sealed and base64url. Kept in step
# with inbox_store by tests/test_inbox.py. The store still cuts a longer
# field rather than refuse it, but a batch of such fields from a client
# that doesn't cut may be refused here (413) instead.
INBOX_MAX_INGEST_BODY_BYTES = (50 * (4 * (120 + 200 + 4000 + 40 + 40) + 512)) * 4 // 3 + 4096
# inbox_store.MAX_DELETE ids of MAX_EVENT_ID_CHARS, each quoted with a
# comma, plus the device id, kid and JSON around them (the same test).
INBOX_MAX_DELETE_BODY_BYTES = 100 * (80 + 3) + 1024

_inbox_key_limiter = core._MintLimiter(INBOX_KEY_LIMIT, INBOX_KEY_WINDOW_S,
                                  message="Too many inbox key requests; try again shortly")
_inbox_ingest_limiter = core._MintLimiter(INBOX_INGEST_LIMIT, INBOX_INGEST_WINDOW_S,
                                     message="Too many inbox events; try again shortly")
_inbox_wipe_limiter = core._MintLimiter(INBOX_WIPE_LIMIT, INBOX_WIPE_WINDOW_S,
                                   message="Too many inbox delete requests; try again shortly")
INBOX_LIST_LIMIT = 60
INBOX_LIST_WINDOW_S = 60.0
_inbox_list_limiter = core._MintLimiter(INBOX_LIST_LIMIT, INBOX_LIST_WINDOW_S,
                                   message="Too many inbox requests; try again shortly")
INBOX_DELETE_LIMIT = 30
INBOX_DELETE_WINDOW_S = 60.0
_inbox_delete_limiter = core._MintLimiter(INBOX_DELETE_LIMIT, INBOX_DELETE_WINDOW_S,
                                     message="Too many inbox delete requests; try again shortly")
_inbox_store_module: Any = None


def _inbox_store() -> Any:
    """inbox_store.py in the plugin's folder, loaded once."""
    global _inbox_store_module
    if _inbox_store_module is None:
        _inbox_store_module = core._plugin_package_module("inbox_store")
    return _inbox_store_module


def _inbox_call(fn: Callable[[Any], Dict[str, Any]]) -> Dict[str, Any]:
    module = _inbox_store()
    try:
        return fn(module.InboxStore(core._calls_home()))
    except module.InboxError as exc:
        raise core.TokenError(exc.status, str(exc), exc.code)
    except sqlite3.DatabaseError:
        logger.warning("conduit_push: inbox store unreadable", exc_info=True)
        raise core.TokenError(503, "The inbox on this host can't be read")


async def _inbox_route(response: Response, profile: Optional[str], fn: Callable[[], Dict[str, Any]],
                       name: str) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        return {"ok": True, **(await core._run_scoped(profile, fn))}
    except core.TokenError as exc:
        return _store_refusal(exc)
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise core._unexpected(name, exc, feature="Shortcuts inbox")


@router.get("/inbox/status")
async def get_inbox_status(response: Response, profile: Optional[str] = None,
                           device_id: Optional[str] = None) -> Dict[str, Any]:
    return await _inbox_route(response, profile, lambda: _inbox_call(lambda store: {
        **store.status(device_id), "crypto": _inbox_store().crypto_available()}), "status")


@router.post("/inbox/key")
async def post_inbox_key(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    await core._scoped_acquire(_inbox_key_limiter, profile)
    body = await core._read_json_body(request, INBOX_MAX_KEY_BODY_BYTES)
    return await _inbox_route(response, profile, lambda: _inbox_call(lambda store: store.register_key(body)), "key")


@router.post("/inbox/ingest")
async def post_inbox_ingest(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    await core._scoped_acquire(_inbox_ingest_limiter, profile)
    body = await core._read_json_body(request, INBOX_MAX_INGEST_BODY_BYTES)
    return await _inbox_route(response, profile, lambda: _inbox_ingest(body), "upload")


def _inbox_ingest(body: Any) -> Dict[str, Any]:
    result = _inbox_call(lambda store: store.ingest(body))
    if result.get("stored"):
        _inbox_tell_pulse()
    return result


def _inbox_tell_pulse() -> None:
    """New events in: Pulse checks the user's open watches against them
    (pulse_followup.inbox_arrived). Only where Pulse runs, on this home;
    the events are stored whatever happens here."""
    try:
        pulse_store = core._plugin_package_module("pulse_store")
        followup = core._plugin_package_module("pulse_followup")
        home = Path(core._calls_home())
        if not (home / pulse_store.DB_NAME).exists():
            return  # Pulse never turned on: leave no file behind
        if followup.inbox_arrived(pulse_store.open_store(home), time.time()):
            _wake_running_pulse()
    except Exception:  # noqa: BLE001 — logging an event never fails on Pulse
        logger.warning("conduit_push: inbox couldn't tell Pulse about new events", exc_info=True)


def _wake_running_pulse() -> int:
    """Wakes the Pulse loop running in this process: the copy Hermes loaded
    with the plugin (pulse.register started its thread), not an import of
    the dashboard's own, whose wake event no loop waits on. A loop in
    another process finds the marker inbox_arrived left on its next tick
    (pulse.TICK_S). How many loops it woke."""
    woken = 0
    for name, module in list(sys.modules.items()):
        if module is None or name.rsplit(".", 1)[-1] != "pulse":
            continue
        thread, wake = getattr(module, "_thread", None), getattr(module, "wake", None)
        if callable(wake) and isinstance(thread, threading.Thread) and thread.is_alive():
            wake()
            woken += 1
    return woken


@router.post("/inbox/list")
async def post_inbox_list(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    await core._scoped_acquire(_inbox_list_limiter, profile)
    body = await core._read_json_body(request, INBOX_MAX_KEY_BODY_BYTES)
    return await _inbox_route(response, profile, lambda: _inbox_call(lambda store: store.list_sealed(body)), "list")


@router.post("/inbox/delete")
async def post_inbox_delete(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    await core._scoped_acquire(_inbox_delete_limiter, profile)
    body = await core._read_json_body(request, INBOX_MAX_DELETE_BODY_BYTES)
    return await _inbox_route(response, profile, lambda: _inbox_delete(body), "delete")


def _inbox_delete(body: Any) -> Dict[str, Any]:
    result = _inbox_call(lambda store: store.delete_events(body))
    _inbox_forget_pulse(body["device_id"], body["ids"])  # delete_events checked both
    return result


def _inbox_wipe(device_id: Optional[str]) -> Dict[str, Any]:
    result = _inbox_call(lambda store: store.wipe(device_id))
    _inbox_forget_pulse(device_id)
    return result


def _inbox_forget_pulse(device_id: Optional[str], events: Optional[list] = None) -> None:
    """Events deleted (``events`` of a device's, all of one device's, or
    every one with no device): the deadlines Pulse memory kept from them go
    at once (pulse_memory.forget_deleted), whether or not Pulse is on. The
    events are deleted whatever happens here, and Pulse's loop catches what
    this couldn't (pulse_memory.forget_inbox)."""
    try:
        core._plugin_package_module("pulse_memory").forget_deleted(Path(core._calls_home()), device_id, events)
    except Exception:  # noqa: BLE001 — deleting events never fails on Pulse
        logger.warning("conduit_push: inbox couldn't tell Pulse about deleted events", exc_info=True)


@router.post("/inbox/wipe")
async def post_inbox_wipe(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    await core._scoped_acquire(_inbox_wipe_limiter, profile)
    body = await core._read_json_body(request, INBOX_MAX_KEY_BODY_BYTES)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object", headers={"Cache-Control": "no-store"})
    # Everything only when asked for by name; a device id alone deletes that device's events.
    if body.get("all") is True and body.get("device_id") is not None:
        raise HTTPException(status_code=400, detail="Name a device_id or all: true, not both",
                            headers={"Cache-Control": "no-store"})
    device_id = None if body.get("all") is True else body.get("device_id")
    if device_id is None and body.get("all") is not True:
        raise HTTPException(status_code=400, detail="Name a device_id, or all: true",
                            headers={"Cache-Control": "no-store"})
    return await _inbox_route(response, profile, lambda: _inbox_wipe(device_id), "delete")


# A maps lookup the agent asked the phone for (conduit_maps): the phone,
# woken by a location push, fetches the lookup sealed for it alone and posts
# its Apple Maps answer sealed back. Neither passes through the relay.
LOCATION_MAPS_LIMIT = 120
LOCATION_MAPS_WINDOW_S = 60.0
LOCATION_MAX_MAPS_BODY_BYTES = 96 * 1024
_location_maps_limiter = core._MintLimiter(LOCATION_MAPS_LIMIT, LOCATION_MAPS_WINDOW_S,
                                      message="Too many maps answers; try again shortly")


@router.get("/location/maps/{request_id}")
async def get_location_maps(request_id: str, response: Response, profile: Optional[str] = None,
                            device_id: Optional[str] = None, kid: Optional[str] = None) -> Dict[str, Any]:
    await core._scoped_acquire(_location_maps_limiter, profile)
    return await _location_route(response, profile, lambda: _location_call(
        lambda store: store.maps_request(request_id, device_id, kid)), "maps request")


@router.post("/location/maps/{request_id}")
async def post_location_maps(request_id: str, request: Request, response: Response,
                             profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    await core._scoped_acquire(_location_maps_limiter, profile)
    body = await core._read_json_body(request, LOCATION_MAX_MAPS_BODY_BYTES)
    return await _location_route(response, profile, lambda: _location_call(
        lambda store: store.answer_maps(request_id, body)), "maps answer")


@router.get("/location/reads")
async def get_location_reads(response: Response, profile: Optional[str] = None, limit: int = 50) -> Dict[str, Any]:
    return await _location_route(response, profile,
                                 lambda: _location_call(lambda store: {"reads": store.reads(limit)}), "reads")


@router.get("/location/rules")
async def get_location_rules(response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    def rules(store: Any) -> Dict[str, Any]:
        keys = ("id", "what", "place_id", "place_name", "event", "days", "hours", "call", "task", "repeat", "last_fired")
        return {"rules": [{key: rule[key] for key in keys} for rule in store.rules()]}

    return await _location_route(response, profile, lambda: _location_call(rules), "rules")


@router.delete("/location/rules/{rule_id}")
async def delete_location_rule(rule_id: str, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    await core._scoped_acquire(_location_wipe_limiter, profile)
    return await _location_route(response, profile, lambda: _location_remove_rule(rule_id), "rule delete")


@router.post("/location/wipe")
async def post_location_wipe(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    await core._scoped_acquire(_location_wipe_limiter, profile)
    body = await core._read_json_body(request, LOCATION_MAX_KEY_BODY_BYTES)
    no_store = {"Cache-Control": "no-store"}
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object", headers=no_store)
    # Everything only when asked for by name; a device id alone deletes that device's data.
    if body.get("all") is True and body.get("device_id") is not None:
        raise HTTPException(status_code=400, detail="Name a device_id or all: true, not both", headers=no_store)
    device_id = None if body.get("all") is True else body.get("device_id")
    if device_id is None and body.get("all") is not True:
        raise HTTPException(status_code=400, detail="Name a device_id, or all: true", headers=no_store)
    return await _location_route(response, profile, lambda: _location_wipe(device_id), "delete")


# --- Calendar, Reminders, Focus and phone status (shared from Conduit) ------
#
# The user turns on each kind in Conduit. The phone agrees a key with this
# profile (/phone/key, X25519, its own key: wiping this never touches health
# or location), posts sealed batches of events, reminders, its calendars,
# Focus and status (/phone/ingest) straight here, never through the push
# relay, and runs a sealed exchange (/phone/exchange) to report how the
# changes Hermes asked for went and to pick up the ones still waiting. The
# store (phone_store.py) keeps it all in the profile's conduit-phone.sqlite3
# for the conduit_calendar and conduit_phone tools. Status reports counts and
# dates only, wipe deletes, and the exchange answers only the device that
# asks, sealed with that device's own key. (hermes-conduit
# designs/phone-v1-contract.md)

PHONE_KEY_LIMIT = 10
PHONE_KEY_WINDOW_S = 60.0
PHONE_WIPE_LIMIT = 5
PHONE_WIPE_WINDOW_S = 60.0
# A first sync or a window change sends many batches in a row.
PHONE_INGEST_LIMIT = 120
PHONE_INGEST_WINDOW_S = 60.0
PHONE_EXCHANGE_LIMIT = 60
PHONE_EXCHANGE_WINDOW_S = 60.0
PHONE_MAX_KEY_BODY_BYTES = 4 * 1024
PHONE_MAX_INGEST_BODY_BYTES = 4 * 1024 * 1024
PHONE_MAX_EXCHANGE_BODY_BYTES = 64 * 1024

_phone_key_limiter = core._MintLimiter(PHONE_KEY_LIMIT, PHONE_KEY_WINDOW_S,
                                  message="Too many calendar key requests; try again shortly")
_phone_ingest_limiter = core._MintLimiter(PHONE_INGEST_LIMIT, PHONE_INGEST_WINDOW_S,
                                     message="Too many calendar uploads; try again shortly")
_phone_exchange_limiter = core._MintLimiter(PHONE_EXCHANGE_LIMIT, PHONE_EXCHANGE_WINDOW_S,
                                       message="Too many calendar change checks; try again shortly")
_phone_wipe_limiter = core._MintLimiter(PHONE_WIPE_LIMIT, PHONE_WIPE_WINDOW_S,
                                   message="Too many calendar delete requests; try again shortly")
_phone_store_module: Any = None


def _phone_store() -> Any:
    """phone_store.py in the plugin's folder, loaded once (as part of the
    private package, like health_store, so a voice read can share it)."""
    global _phone_store_module
    if _phone_store_module is None:
        _phone_store_module = core._plugin_package_module("phone_store")
    return _phone_store_module


def _phone_call(fn: Callable[[Any], Dict[str, Any]]) -> Dict[str, Any]:
    module = _phone_store()
    try:
        return fn(module.PhoneStore(core._calls_home()))
    except module.PhoneError as exc:
        raise core.TokenError(exc.status, str(exc), exc.code)


async def _phone_route(response: Response, profile: Optional[str], fn: Callable[[], Dict[str, Any]],
                       name: str) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        return {"ok": True, **(await core._run_scoped(profile, fn))}
    except core.TokenError as exc:
        return _store_refusal(exc)
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise core._unexpected(name, exc, feature="Calendar sharing")


@router.get("/phone/status")
async def get_phone_status(response: Response, profile: Optional[str] = None,
                           device_id: Optional[str] = None) -> Dict[str, Any]:
    return await _phone_route(response, profile, lambda: _phone_call(lambda store: {
        **store.status(device_id), "crypto": _phone_store().crypto_available()}), "status")


@router.post("/phone/key")
async def post_phone_key(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    await core._scoped_acquire(_phone_key_limiter, profile)
    body = await core._read_json_body(request, PHONE_MAX_KEY_BODY_BYTES)
    return await _phone_route(response, profile, lambda: _phone_call(lambda store: store.register_key(body)), "key")


@router.post("/phone/ingest")
async def post_phone_ingest(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    await core._scoped_acquire(_phone_ingest_limiter, profile)
    body = await core._read_json_body(request, PHONE_MAX_INGEST_BODY_BYTES)
    return await _phone_route(response, profile, lambda: _phone_call(lambda store: store.ingest(body)), "upload")


@router.post("/phone/exchange")
async def post_phone_exchange(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    await core._scoped_acquire(_phone_exchange_limiter, profile)
    body = await core._read_json_body(request, PHONE_MAX_EXCHANGE_BODY_BYTES)
    return await _phone_route(response, profile, lambda: _phone_call(lambda store: store.exchange(body)), "exchange")


@router.post("/phone/wipe")
async def post_phone_wipe(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    await core._scoped_acquire(_phone_wipe_limiter, profile)
    body = await core._read_json_body(request, PHONE_MAX_KEY_BODY_BYTES)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object", headers={"Cache-Control": "no-store"})
    # Everything only when asked for by name; a device id alone deletes that device's data.
    if body.get("all") is True and body.get("device_id") is not None:
        raise HTTPException(status_code=400, detail="Name a device_id or all: true, not both",
                            headers={"Cache-Control": "no-store"})
    device_id = None if body.get("all") is True else body.get("device_id")
    if device_id is None and body.get("all") is not True:
        raise HTTPException(status_code=400, detail="Name a device_id, or all: true",
                            headers={"Cache-Control": "no-store"})
    return await _phone_route(response, profile, lambda: _phone_call(lambda store: store.wipe(device_id)), "delete")
