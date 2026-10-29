"""Linked Rooms: your agent's choices in each room, links, invites, drafts,
approvals and room setup.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from fastapi import HTTPException, Request, Response

from . import core, phone_data

logger = core.logger
router = core.router


# --- Linked Rooms: your agent's choices in each room ----------------------------
#
# What each of this profile's agents may do in a room with other people's
# agents (room_choices.py), enforced on this host by the plugin's hooks
# (room_rules.py). Conduit shows them in the room's info and when the agent
# joins a room.

ROOM_CHOICES_LIMIT = 60
ROOM_CHOICES_WINDOW_S = 60.0
ROOM_CHOICES_MAX_BODY_BYTES = 16 * 1024

_room_choices_limiter = core._MintLimiter(ROOM_CHOICES_LIMIT, ROOM_CHOICES_WINDOW_S,
                                     message="Too many room choice changes; try again shortly")
_room_choices_module: Any = None


def _room_choices() -> Any:
    """room_choices.py in the plugin's folder, loaded once."""
    global _room_choices_module
    with core._calls_store_lock:
        if _room_choices_module is None:
            path = os.path.join(core._plugin_root(), "room_choices.py")
            name = "conduit_push_dashboard_room_choices"
            spec = importlib.util.spec_from_file_location(name, path)
            if spec is None or spec.loader is None:
                raise core.TokenError(501, "This plugin install is missing room_choices.py; update the plugin")
            module = importlib.util.module_from_spec(spec)
            # Its dataclasses look their module up by name while they're made.
            sys.modules[name] = module
            try:
                spec.loader.exec_module(module)
            except BaseException:
                sys.modules.pop(name, None)
                raise
            _room_choices_module = module
        return _room_choices_module


def room_choices(action: str, room_id: Any = None, choices: Any = None) -> Dict[str, Any]:
    module = _room_choices()
    store = module.ChoicesStore(core._calls_home())
    try:
        if action == "defaults":
            return {"choices": store.defaults().as_dict()}
        if action == "set_defaults":
            return {"choices": store.set_defaults(choices).as_dict()}
        if action == "set":
            return {"room_id": room_id, "choices": store.set(room_id, choices).as_dict()}
        if room_id is None:
            return store.view()
        return {"room_id": room_id, "choices": store.get(room_id).as_dict(), "saved": store.saved(room_id)}
    except module.ChoicesError as exc:
        raise core.TokenError(400, str(exc))


async def _room_choices_route(profile: Optional[str], run: Callable[[], Dict[str, Any]], *,
                              limiter: Optional["core._MintLimiter"] = None, route: str = "room choices") -> Dict[str, Any]:
    try:
        return {"ok": True, **(await core._run_scoped(profile, core._limited(limiter or _room_choices_limiter, profile, run)))}
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise core._unexpected(route, exc, feature="Linked Rooms")


def _room_choices_body(body: Any, *, room: bool) -> tuple[Any, Any]:
    if not isinstance(body, dict) or "choices" not in body or (room and "room_id" not in body):
        raise HTTPException(status_code=400, headers={"Cache-Control": "no-store"},
                            detail="Expected {\"room_id\": ..., \"choices\": {...}}" if room
                            else "Expected {\"choices\": {...}}")
    return body.get("room_id"), body["choices"]


@router.get("/rooms/choices")
async def get_room_choices(response: Response, room_id: Optional[str] = None,
                           profile: Optional[str] = None) -> Dict[str, Any]:
    """One room's choices (``saved`` false: the defaults, since it has none),
    or every room's with the defaults when no room is named."""
    response.headers["Cache-Control"] = "no-store"
    return await _room_choices_route(profile, lambda: room_choices("get", room_id))


@router.put("/rooms/choices")
async def put_room_choices(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    """Changes the choices the body names for one room; the others stay."""
    response.headers["Cache-Control"] = "no-store"
    room_id, choices = _room_choices_body(await core._read_json_body(request, ROOM_CHOICES_MAX_BODY_BYTES), room=True)
    return await _room_choices_route(profile, lambda: room_choices("set", room_id, choices))


@router.get("/rooms/defaults")
async def get_room_defaults(response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return await _room_choices_route(profile, lambda: room_choices("defaults"))


@router.put("/rooms/defaults")
async def put_room_defaults(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    _, choices = _room_choices_body(await core._read_json_body(request, ROOM_CHOICES_MAX_BODY_BYTES), room=False)
    return await _room_choices_route(profile, lambda: room_choices("set_defaults", choices=choices))


# Deleting this profile's hidden chats for one room (room_cleanup.py): the
# home deletes its own agents' when a room is disbanded, and a member host's
# owner deletes theirs here, from an ended room hosted elsewhere.

ROOM_FORGET_LIMIT = 10
_room_forget_limiter = core._MintLimiter(ROOM_FORGET_LIMIT, ROOM_CHOICES_WINDOW_S,
                                    message="Too many room chat deletes; try again shortly")


def room_forget(room_id: Any) -> Dict[str, Any]:
    module = core._plugin_package_module("room_cleanup")
    try:
        return module.forget_room(room_id, Path(core._calls_home()))
    except module.CleanupError as exc:
        raise core.TokenError(exc.status, str(exc))


@router.delete("/rooms/sessions")
async def delete_room_sessions(response: Response, room_id: Optional[str] = None,
                               profile: Optional[str] = None) -> Dict[str, Any]:
    """Deletes this profile's hidden chats for a room, with their earlier
    history; ``kept`` counts any a turn is still finishing in. A room still
    going here is refused (409): disbanding it ends them."""
    response.headers["Cache-Control"] = "no-store"
    return await _room_choices_route(profile, lambda: room_forget(room_id), limiter=_room_forget_limiter,
                                     route="room chats")


# A room's private chat with one of this profile's agents (room_chats.py):
# Conduit's "Ask your agent about this room" names the chat it opened, and
# each turn there is told which room it's about.

ROOM_CHATS_MAX_BODY_BYTES = 4 * 1024


def room_chats(action: str, room_id: Any = None, chat_id: Any = None) -> Dict[str, Any]:
    module = core._plugin_package_module("room_chats")
    home = Path(core._calls_home())
    if action == "list":
        return module.Store(home).view()
    try:
        return {"chat": module.open_chat(home, room_id, chat_id)}
    except module.ChatsError as exc:
        raise core.TokenError(404 if exc.code == "no_room" else 503 if exc.code == "unreadable" else 400, str(exc))


@router.get("/rooms/chats")
async def get_room_chats(response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    """This profile's room chats: each room's, with the seq its notes reach."""
    response.headers["Cache-Control"] = "no-store"
    return await _room_choices_route(profile, lambda: room_chats("list"), route="room chats")


@router.put("/rooms/chats")
async def put_room_chat(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    """``chat_id`` is this profile's chat about ``room_id`` from now on (the
    latest kept). A room new to its chats gets notes on what comes after its
    newest message; one it had keeps its notes where they were."""
    response.headers["Cache-Control"] = "no-store"
    body = await core._read_json_body(request, ROOM_CHATS_MAX_BODY_BYTES)
    if not isinstance(body, dict) or "room_id" not in body or "chat_id" not in body:
        raise HTTPException(status_code=400, headers={"Cache-Control": "no-store"},
                            detail="Expected {\"room_id\": ..., \"chat_id\": ...}")
    return await _room_choices_route(profile, lambda: room_chats("open", body["room_id"], body["chat_id"]),
                                     route="room chats")


# Links between installations (room_links.py): invites, the Links they make
# and the loop that reads this installation's inbox at the rooms mailbox.
# Links belong to the installation, in its root home, so these routes take no
# profile. The loop runs in the process serving the dashboard (decision 3):
# it starts when this module loads there, and from any links route, which
# also wakes it, so a new invite or Link is read at once.

# As many as room choices allow: a screen waiting on a Link's confirmation
# asks for the list every few seconds.
ROOM_LINKS_LIMIT = 60
ROOM_LINKS_MAX_BODY_BYTES = 8 * 1024
_room_links_limiter = core._MintLimiter(ROOM_LINKS_LIMIT, ROOM_CHOICES_WINDOW_S,
                                   message="Too many link requests; try again shortly")
# Inviting, accepting, unlinking and cancelling wait on the rooms mailbox, up
# to 15 s a call. They get their own small pool, so a mailbox that doesn't
# answer holds at most this many threads and never starves the dashboard's
# shared executor, and an answer within the time below; a call still running
# then finishes on its own.
ROOM_LINKS_WORKERS = 4
ROOM_LINKS_TIMEOUT_S = 50.0
_room_links_executor = ThreadPoolExecutor(max_workers=ROOM_LINKS_WORKERS, thread_name_prefix="conduit-room-links")


def _room_links_root() -> Path:
    return core._plugin_package_module("room_identity").root_home(core._calls_home())


def _start_room_handlers(root: Path) -> None:
    """What takes the Links' envelopes, started before the loop so an
    envelope it reads finds its handler: the bridge that carries room turns
    (room_bridge.py), rooms over Links (room_invites.py), whose loop also
    starts the rooms whose service runs in this process, and the asks between
    linked agents (link_asks.py)."""
    try:
        core._plugin_package_module("room_bridge").start(root)
    except Exception:  # noqa: BLE001 - the Links go on without it
        logger.warning("Conduit plugin: couldn't start Linked Rooms' bridge; a links route tries again",
                       exc_info=True)
    try:
        core._plugin_package_module("room_invites").start(root)
    except Exception:  # noqa: BLE001 - the Links go on without them
        logger.warning("Conduit plugin: couldn't start Linked Rooms' rooms loop; a links or rooms route tries "
                       "again", exc_info=True)
    try:
        core._plugin_package_module("link_asks").start(root)
    except Exception:  # noqa: BLE001 - the Links go on without them
        logger.warning("Conduit plugin: couldn't start the asks between linked agents; a links route tries again",
                       exc_info=True)


def _start_room_links() -> None:
    if "hermes_cli.web_server" not in sys.modules:
        return  # not the dashboard (tests, a plugin host process): a links route starts it
    try:
        root = _room_links_root()
        _start_room_handlers(root)
        core._plugin_package_module("room_links").start(root)
    except Exception:  # noqa: BLE001 - Linked Rooms only, never the routes
        logger.warning("Conduit plugin: couldn't start Linked Rooms' mailbox loop; a links route tries again",
                       exc_info=True)


def room_links(action: Callable[[Any], Dict[str, Any]]) -> Dict[str, Any]:
    module = core._plugin_package_module("room_links")
    root = _room_links_root()
    _start_room_handlers(root)
    try:
        loop = module.start(root)
    except Exception:  # noqa: BLE001 - the route still answers; the next one tries again
        logger.warning("Conduit plugin: couldn't start Linked Rooms' mailbox loop", exc_info=True)
        loop = None
    try:
        answer = action(module.Links(root))
    except module.LinkError as exc:
        raise core.TokenError(exc.status, str(exc), exc.code)
    if loop is not None:
        loop.wake()
    return answer


async def _room_links_route(action: Callable[[Any], Dict[str, Any]], route: str, *, mailbox: bool = False) -> Any:
    """``mailbox``: the action waits on the rooms mailbox, so it runs in the
    links pool, for up to ROOM_LINKS_TIMEOUT_S. Its slot of the limit is
    taken first, so one waiting there for a thread counts too."""
    try:
        if mailbox:
            await core._run_scoped(None, lambda: _room_links_limiter.acquire(core._limiter_key(None)))
            answer = await asyncio.wait_for(core._run_scoped(None, lambda: room_links(action), _room_links_executor),
                                            timeout=ROOM_LINKS_TIMEOUT_S)
        else:
            answer = await core._run_scoped(None, core._limited(_room_links_limiter, None, lambda: room_links(action)))
        return {"ok": True, **answer}
    except asyncio.TimeoutError:
        logger.warning("Linked Rooms' %s for Conduit timed out after %ss", route, ROOM_LINKS_TIMEOUT_S)
        return phone_data._store_refusal(core.TokenError(504, "The rooms mailbox didn't answer in time; look at the Links again in "
                                              "a minute", "timed_out"))
    except core.TokenError as exc:
        # ``code`` is room_links' word for the refusal (bad_invite, expired,
        # invite_used, already_linked, clock_off…), so Conduit can say it.
        return phone_data._store_refusal(exc)
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise core._unexpected(route, exc, feature="Linked Rooms")


def _room_links_body(body: Any, expected: str) -> Dict[str, Any]:
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail=f"Expected {expected}", headers={"Cache-Control": "no-store"})
    return body


@router.get("/rooms/identity")
async def get_room_identity(response: Response) -> Any:
    """This installation's four fingerprint words, for two people to compare
    in person: ``words`` is null until its first invite or accept makes its
    keys, and ``crypto`` false on a Hermes without the cryptography package."""
    response.headers["Cache-Control"] = "no-store"
    return await _room_links_route(lambda links: links.words(), "identity")


@router.post("/rooms/links/invites")
async def post_room_link_invite(request: Request, response: Response) -> Any:
    """A new invite, from ``name`` (who the other person sees it's from): its
    ``conduit://link/`` URL, also the QR code's payload, for one accept within
    24 hours."""
    response.headers["Cache-Control"] = "no-store"
    body = _room_links_body(await core._read_json_body(request, ROOM_LINKS_MAX_BODY_BYTES), '{"name": ...}')
    return await _room_links_route(lambda links: links.invite(body.get("name")), "invite", mailbox=True)


@router.post("/rooms/links/preview")
async def post_room_link_preview(request: Request, response: Response) -> Any:
    """Who an invite is from and their words, whether it ran out, whether
    it's this Hermes' own, and the Link this Hermes already has with them."""
    response.headers["Cache-Control"] = "no-store"
    body = _room_links_body(await core._read_json_body(request, ROOM_LINKS_MAX_BODY_BYTES), '{"url": ...}')
    return await _room_links_route(lambda links: links.preview(body.get("url")), "preview")


@router.post("/rooms/links")
async def post_room_link(request: Request, response: Response) -> Any:
    """Accepts an invite, as ``name``: the Link, waiting until the inviter's
    Hermes confirms it."""
    response.headers["Cache-Control"] = "no-store"
    body = _room_links_body(await core._read_json_body(request, ROOM_LINKS_MAX_BODY_BYTES),
                            '{"url": ..., "name": ...}')
    return await _room_links_route(lambda links: links.accept(body.get("url"), body.get("name")), "accept",
                                   mailbox=True)


@router.get("/rooms/links")
async def get_room_links(response: Response) -> Any:
    """The Links, the invites still waiting, and the mailbox: ``reading`` is
    false while nothing on this Hermes reads it."""
    response.headers["Cache-Control"] = "no-store"
    return await _room_links_route(lambda links: links.view(), "list")


@router.delete("/rooms/links")
async def delete_room_link(response: Response, link_id: Optional[str] = None) -> Any:
    """Unlinks; the other side's Hermes drops the Link when it reads why."""
    response.headers["Cache-Control"] = "no-store"
    return await _room_links_route(lambda links: links.remove(link_id), "unlink", mailbox=True)


@router.delete("/rooms/links/invites")
async def delete_room_link_invite(response: Response, invite_id: Optional[str] = None) -> Any:
    """Takes an invite back before it's used."""
    response.headers["Cache-Control"] = "no-store"
    return await _room_links_route(lambda links: links.cancel(invite_id), "cancel", mailbox=True)


# Rooms with people you've linked (room_invites.py): the home's drafts,
# which start by themselves once everyone invited has answered, and the
# invites this installation was sent, with the join card; then, while a room
# goes on, taking agents out of it, yours (leave) or theirs (remove). Both are
# the installation's, in its root home, so only a new draft takes a profile:
# its room starts in the room service that profile's requests use
# (room_start's where_from), which only the dashboard process has, so a
# profile it didn't start with comes as ?conduit_profile= (_conduit_route_profile,
# hermes-conduit#1336). The rooms loop starts with the bridge, before the
# Links' loop (_start_room_handlers), starts the drafts whose service runs
# here, and renews this installation's agents' grants in rooms elsewhere.

ROOM_INVITES_LIMIT = 60
# Room for a draft's or an answer's agents with their room choices.
ROOM_INVITES_MAX_BODY_BYTES = 64 * 1024
_room_invites_limiter = core._MintLimiter(ROOM_INVITES_LIMIT, ROOM_CHOICES_WINDOW_S,
                                     message="Too many room requests; try again shortly")


def room_invites(action: Callable[[Any], Dict[str, Any]]) -> Dict[str, Any]:
    module = core._plugin_package_module("room_invites")
    root = _room_links_root()
    _start_room_handlers(root)
    try:
        loop = core._plugin_package_module("room_links").start(root)
    except Exception:  # noqa: BLE001 - the route still answers; the next one tries again
        logger.warning("Conduit plugin: couldn't start Linked Rooms' mailbox loop", exc_info=True)
        loop = None
    posts = core._plugin_package_module("room_posts")
    try:
        answer = action(module.start(root))
    except (module.RoomsError, posts.PostsError) as exc:
        raise core.TokenError(exc.status, str(exc), exc.code)
    if loop is not None:
        loop.wake()
    return answer


async def _room_invites_route(profile: Optional[str], action: Callable[[Any], Dict[str, Any]], route: str, *,
                              mailbox: bool = False) -> Any:
    """``mailbox``: the action sends over the Links, so it runs in the links
    pool, for up to ROOM_LINKS_TIMEOUT_S, as the links routes that do."""
    try:
        if mailbox:
            await core._run_scoped(profile, lambda: _room_invites_limiter.acquire(core._limiter_key(None)))
            answer = await asyncio.wait_for(core._run_scoped(profile, lambda: room_invites(action), _room_links_executor),
                                            timeout=ROOM_LINKS_TIMEOUT_S)
        else:
            answer = await core._run_scoped(profile, core._limited(_room_invites_limiter, None, lambda: room_invites(action)))
        return {"ok": True, **answer}
    except asyncio.TimeoutError:
        logger.warning("Linked Rooms' %s for Conduit timed out after %ss", route, ROOM_LINKS_TIMEOUT_S)
        return phone_data._store_refusal(core.TokenError(504, "The rooms mailbox didn't answer in time; look at the room again in "
                                              "a minute", "timed_out"))
    except core.TokenError as exc:
        # ``code`` is room_invites' word for the refusal (bad_agents,
        # not_linked, not_set_up, codex, lapsed, answered…).
        return phone_data._store_refusal(exc)
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise core._unexpected(route, exc, feature="Linked Rooms")


def _room_draft_where(request: Request, profile: Optional[str]) -> Dict[str, Any]:
    """Where a draft this request makes starts (room_start.where_from)."""
    module = core._plugin_package_module("room_start")
    try:
        return module.where_from(request, profile)
    except module.StartError as exc:
        raise core.TokenError(exc.status, str(exc), exc.code)


def _room_people(rooms: Any, room_id: Any) -> Dict[str, Any]:
    people = core._plugin_package_module("room_invites").people(rooms.root, room_id)
    if people is None:
        raise core.TokenError(404, "There's no room over Links with that id here", "no_room")
    return people


@router.post("/rooms/drafts")
async def post_room_draft(request: Request, response: Response, profile: Optional[str] = None) -> Any:
    """A room with people you've linked, waiting for their answers: its
    ``name``, your ``agents`` (each ``{profile, handle, display_name}``, with
    its ``choices`` for the room, or null for those of your last room with
    the same people) and the ``links`` of the people invited. It starts by
    itself once they've all answered, in the room service this profile's
    requests use."""
    response.headers["Cache-Control"] = "no-store"
    profile = await core._conduit_route_profile_no_store(request, profile)
    body = _room_links_body(await core._read_json_body(request, ROOM_INVITES_MAX_BODY_BYTES),
                            '{"name": ..., "agents": [...], "links": [...]}')
    try:
        where = _room_draft_where(request, profile)
    except core.TokenError as exc:
        return phone_data._store_refusal(exc)
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    return await _room_invites_route(profile, lambda rooms: {"draft": rooms.draft(
        body.get("name"), body.get("agents"), body.get("links"), where=where)}, "draft", mailbox=True)


@router.get("/rooms/drafts")
async def get_room_drafts(response: Response) -> Any:
    """The rooms this Hermes hosts with people you've linked, newest first:
    waiting for answers, started or ended, each with who's invited and how
    they answered; ``starts`` says whether this Hermes can start them."""
    response.headers["Cache-Control"] = "no-store"
    return await _room_invites_route(None, lambda rooms: rooms.drafts(), "drafts")


@router.post("/rooms/drafts/start")
async def post_room_draft_start(request: Request, response: Response) -> Any:
    """Start now: the room starts with whoever has joined, and the rest are
    told it started without them."""
    response.headers["Cache-Control"] = "no-store"
    body = _room_links_body(await core._read_json_body(request, ROOM_LINKS_MAX_BODY_BYTES), '{"room_id": ...}')
    return await _room_invites_route(None, lambda rooms: {"draft": rooms.start(body.get("room_id"))}, "start")


@router.delete("/rooms/drafts")
async def delete_room_draft(response: Response, room_id: Optional[str] = None) -> Any:
    """Takes back a room still waiting for answers: everyone invited who
    hasn't declined is told, and their Hermes undoes their joins."""
    response.headers["Cache-Control"] = "no-store"
    return await _room_invites_route(None, lambda rooms: {"draft": rooms.withdraw(room_id)}, "withdraw",
                                     mailbox=True)


@router.get("/rooms/invites")
async def get_room_invites(response: Response) -> Any:
    """The join card: each room this Hermes was invited to over a Link,
    newest first, with who it's from, the other people and agents in it,
    and each of your profiles' choices for it, from your last room with the
    same people or the defaults."""
    response.headers["Cache-Control"] = "no-store"
    return await _room_invites_route(None, lambda rooms: {"invites": rooms.invites()}, "invites")


@router.post("/rooms/invites/answer")
async def post_room_invite_answer(request: Request, response: Response) -> Any:
    """Joins the room ``room_id`` the Link ``link`` invited this Hermes to
    with ``agents`` (each ``{profile, handle, display_name}``, with its
    ``choices``, or null for the join card's), or declines it with none."""
    response.headers["Cache-Control"] = "no-store"
    body = _room_links_body(await core._read_json_body(request, ROOM_INVITES_MAX_BODY_BYTES),
                            '{"link": ..., "room_id": ..., "agents": [...]}')
    return await _room_invites_route(None, lambda rooms: {"invite": rooms.answer(
        body.get("link"), body.get("room_id"), body.get("agents"))}, "answer", mailbox=True)


@router.post("/rooms/leave")
async def post_room_leave(request: Request, response: Response) -> Any:
    """Takes your agents out of the started room ``room_id`` the Link
    ``link`` invited this Hermes to: those of the profiles in ``agents``, or
    every one still in (null). Each is undone here and the room's host is
    told, which cuts it off; their hidden chats for the room stay (DELETE
    /rooms/sessions deletes one)."""
    response.headers["Cache-Control"] = "no-store"
    body = _room_links_body(await core._read_json_body(request, ROOM_LINKS_MAX_BODY_BYTES),
                            '{"link": ..., "room_id": ..., "agents": [...] | null}')
    return await _room_invites_route(None, lambda rooms: {"invite": rooms.leave(
        body.get("link"), body.get("room_id"), body.get("agents"))}, "leave", mailbox=True)


@router.post("/rooms/drafts/remove")
async def post_room_draft_remove(request: Request, response: Response) -> Any:
    """Takes a linked person's agent out of a started room this Hermes
    hosts: ``member`` is its member id or @handle. Its route is cut off and
    its grant revoked, and its Hermes is told, which undoes its join."""
    response.headers["Cache-Control"] = "no-store"
    body = _room_links_body(await core._read_json_body(request, ROOM_LINKS_MAX_BODY_BYTES),
                            '{"room_id": ..., "member": ...}')
    return await _room_invites_route(None, lambda rooms: {"draft": rooms.remove(
        body.get("room_id"), body.get("member"))}, "remove", mailbox=True)


@router.get("/rooms/people")
async def get_room_people(response: Response, room_id: Optional[str] = None) -> Any:
    """Who's who in a started room over Links that this Hermes hosts or has
    agents in: ``home``, the host's owner as your Link names them (null when
    it's yours), and each member with its owner's name (null for yours)."""
    response.headers["Cache-Control"] = "no-store"
    return await _room_invites_route(None, lambda rooms: _room_people(rooms, room_id), "people")


# Your agents' approvals from rooms other people host (room_approvals.py):
# listed and answered here, through this Hermes' own API server, never
# through the room's home. Conduit asks for them every few seconds while it
# shows a room, so they have their own 60 a minute. Each read and answer
# waits on that API server, up to 10 s a call, so they run on threads of
# their own, which a slow server holds instead of the dashboard's shared
# executor, and a request that takes longer reads as the server not answering.

ROOM_APPROVALS_LIMIT = 60
ROOM_APPROVALS_WORKERS = 2
ROOM_APPROVALS_TIMEOUT_S = 30.0
_room_approvals_limiter = core._MintLimiter(ROOM_APPROVALS_LIMIT, ROOM_CHOICES_WINDOW_S,
                                       message="Too many approval requests; try again shortly")
_room_approvals_executor = ThreadPoolExecutor(max_workers=ROOM_APPROVALS_WORKERS,
                                              thread_name_prefix="conduit-room-approvals")


def room_approvals(action: Callable[[Any], Dict[str, Any]]) -> Dict[str, Any]:
    module = core._plugin_package_module("room_approvals")
    root = _room_links_root()
    # The bridge's member side watches the turns whose approvals these are.
    _start_room_handlers(root)
    try:
        return action(module.here(root))
    except module.ApprovalsError as exc:
        raise core.TokenError(exc.status, str(exc), exc.code)


async def _room_approvals_route(action: Callable[[Any], Dict[str, Any]], route: str) -> Any:
    try:
        answer = await asyncio.wait_for(
            core._run_scoped(None, core._limited(_room_approvals_limiter, None, lambda: room_approvals(action)),
                        _room_approvals_executor),
            timeout=ROOM_APPROVALS_TIMEOUT_S)
        return {"ok": True, **answer}
    except asyncio.TimeoutError:
        logger.warning("Linked Rooms' %s for Conduit timed out after %ss", route, ROOM_APPROVALS_TIMEOUT_S)
        return phone_data._store_refusal(core.TokenError(503, "This Hermes' API server didn't answer in time; check that its "
                                              "gateway runs", "api_server"))
    except core.TokenError as exc:
        # ``code`` is room_approvals' word for the refusal (bad_choice,
        # no_approval, not_pending, refused, api_server…).
        return phone_data._store_refusal(exc)
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise core._unexpected(route, exc, feature="Linked Rooms")


@router.get("/rooms/approvals")
async def get_room_approvals(response: Response) -> Any:
    """Each approval your agents' turns wait for in rooms other people
    host, the oldest turn's first: its ``run_id`` and ``request_id``, the
    room, who hosts it, the agent by its handle there, what it asks
    (``description``, ``command``) and the ``choices``, once or deny."""
    response.headers["Cache-Control"] = "no-store"
    return await _room_approvals_route(lambda approvals: {"approvals": approvals.waiting()}, "approvals")


@router.post("/rooms/approvals/answer")
async def post_room_approval_answer(request: Request, response: Response) -> Any:
    """Answers the approval ``request_id`` that your agent's turn ``run_id``
    waits for with ``choice``, ``once`` or ``deny``, on this Hermes."""
    response.headers["Cache-Control"] = "no-store"
    body = _room_links_body(await core._read_json_body(request, ROOM_LINKS_MAX_BODY_BYTES),
                            '{"run_id": ..., "request_id": ..., "choice": "once" | "deny"}')
    return await _room_approvals_route(lambda approvals: {"approval": approvals.answer(
        body.get("run_id"), body.get("request_id"), body.get("choice"))}, "approval answer")


# Copies of rooms other people host (room_copies.py): every person whose
# agents are in a room keeps one on their own Hermes, sent by the room's home
# a page at a time, and Conduit reads it here, never from the home. Reading
# one is local, so they share the invites' 60 a minute.


def room_copies(action: Callable[[Any, Path], Dict[str, Any]]) -> Dict[str, Any]:
    module = core._plugin_package_module("room_copies")
    root = _room_links_root()
    # The rooms loop and its handlers take the pages.
    _start_room_handlers(root)
    try:
        return action(module, root)
    except module.CopiesError as exc:
        raise core.TokenError(exc.status, str(exc), exc.code)


async def _room_copies_route(action: Callable[[Any, Path], Dict[str, Any]], route: str) -> Any:
    try:
        answer = await core._run_scoped(None, core._limited(_room_invites_limiter, None, lambda: room_copies(action)))
        return {"ok": True, **answer}
    except core.TokenError as exc:
        # ``code`` is room_copies' word for the refusal (no_copy, bad_room,
        # bad_since, bad_limit).
        return phone_data._store_refusal(exc)
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise core._unexpected(route, exc, feature="Linked Rooms")


@router.get("/rooms/copies")
async def get_room_copies(response: Response) -> Any:
    """Each room hosted elsewhere this Hermes keeps a copy of, the one that
    changed last first: its name, who hosts it and over which ``link``, the
    members with their owners, your agents in it (``yours``), whether it goes
    on (``status``, ``reason``), what it's doing (``state``: ``ended``,
    ``working``, ``activity``, ``routes``) and the seqs the copy holds."""
    response.headers["Cache-Control"] = "no-store"
    return await _room_copies_route(lambda module, root: {"copies": module.copies(root)}, "copies")


@router.get("/rooms/copies/log")
async def get_room_copy_log(response: Response, room_id: Optional[str] = None, since_seq: int = 0,
                            limit: int = 100) -> Any:
    """A copy's events after ``since_seq``, as groups.log pages a room this
    Hermes hosts: ``events``, ``cursor``, ``latest_seq`` (what the copy
    holds), ``has_more`` and the room's ``authority``."""
    response.headers["Cache-Control"] = "no-store"
    return await _room_copies_route(lambda module, root: module.log(root, room_id, since_seq=since_seq, limit=limit),
                                    "copy log")


@router.delete("/rooms/copies")
async def delete_room_copy(response: Response, room_id: Optional[str] = None) -> Any:
    """Forgets this Hermes' copy of a room other people host, once your agents
    are out of it; a page its home sends later starts it again. Refused while
    an agent of yours is still in it (``still_in``, 409), for a room this
    Hermes hosts (``hosted``, 409) and when the copies can't be read
    (``unreadable``, 503)."""
    response.headers["Cache-Control"] = "no-store"
    return await _room_copies_route(lambda module, root: module.forget(root, room_id), "forget copy")


# Posts in rooms other people host (room_posts.py): Conduit posts to its own
# Hermes, which sends each to the room's home over the Link that invited it;
# the home posts it in the room named for its person and says how it went.
# Posting goes over the Links, as the invites' answers do.

ROOM_POSTS_MAX_BODY_BYTES = 64 * 1024


@router.post("/rooms/copies/messages")
async def post_room_copy_message(request: Request, response: Response) -> Any:
    """Posts ``text`` in the room ``room_id`` someone else hosts, which your
    agents are in: sent to its home, which posts it named for you.
    ``event_id`` is the retry key (the same one again is the same post).
    Answers the post as ``GET /rooms/copies/messages`` lists it: ``status``
    sending until the home answers."""
    response.headers["Cache-Control"] = "no-store"
    body = _room_links_body(await core._read_json_body(request, ROOM_POSTS_MAX_BODY_BYTES),
                            '{"room_id": ..., "event_id": ..., "text": ...}')
    return await _room_invites_route(None, lambda rooms: {"message": rooms.posts.post(
        body.get("room_id"), body.get("event_id"), body.get("text"))}, "post", mailbox=True)


@router.get("/rooms/copies/messages")
async def get_room_copy_messages(response: Response, room_id: Optional[str] = None) -> Any:
    """What you posted in the room ``room_id`` someone else hosts, oldest
    first: each ``event_id``, ``text`` and ``status`` (sending, posted with
    the room's ``seq``, or refused with its ``error``: ended, not_in,
    too_long, too_many, unavailable, bad_post)."""
    response.headers["Cache-Control"] = "no-store"
    return await _room_invites_route(None, lambda rooms: {"messages": rooms.posts.listed(room_id)}, "posts")


# Setting this Hermes up for rooms (room_setup.py): the API server, which is
# the installation's, and the profile's room ceiling and approvals, then the
# gateway restart that starts the API server. The limits are the
# installation's too. The setup writes the installation's .env in the
# dashboard's scope and restarts the gateway as the dashboard does, so a
# profile it didn't start with comes as ?conduit_profile= (hermes-conduit#1336).

ROOM_SETUP_LIMIT = 6
ROOM_SETUP_WINDOW_S = 600.0
ROOM_SETUP_READ_LIMIT = 30
ROOM_SETUP_MAX_BODY_BYTES = 8 * 1024
_room_setup_limiter = core._MintLimiter(ROOM_SETUP_LIMIT, ROOM_SETUP_WINDOW_S,
                                   message="Too many setup changes; try again in a few minutes")
_room_setup_read_limiter = core._MintLimiter(ROOM_SETUP_READ_LIMIT, ROOM_CHOICES_WINDOW_S,
                                        message="Too many setup checks; try again shortly")
_ROOM_SETUP_CANT_RESTART = "This Hermes can't restart its gateway from here; restart it on the host (hermes gateway restart)"
_ROOM_SETUP_RESTART_FAILED = ("The gateway didn't restart (this Hermes' log says why); restart it on the host "
                              "(hermes gateway restart)")
# Hermes 0.22.3+ runs a profile's plugins apart from the dashboard, in a
# plugin host (an older Conduit's ?profile= setup for a profile the dashboard
# didn't start with lands there). A restart started there brings the gateway
# back with the host's environment, Hermes' plugin host flag among it, which
# has Hermes load every plugin in-process (on Hermes c73cbcc4). So the setup
# is saved there, and the gateway is left to be restarted.
_ROOM_SETUP_HOST_RESTART = ("Hermes runs this profile's plugins apart from its dashboard, where its gateway can't "
                            "be restarted safely; restart it on the host (hermes gateway restart), or update Conduit")


def _room_setup() -> Any:
    return core._plugin_package_module("room_setup")


def _room_setup_hermes(module: Any) -> Any:
    """Hermes' calls for the switch. Each .env write is made in the scope of
    the profile whose home it is: the root's (the API server's key and port)
    in the default profile's, a named profile's own key in that profile's."""
    return module.Hermes.load(env_scope=lambda home: core._profile_scope(module.profile_name(home)))


def _optional_module(name: str) -> Any:
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


def _restart_room_gateway(interrupt: bool) -> Dict[str, Any]:
    """Restarts the host gateway (the default profile's), which starts the
    API server: Hermes' own restart after a settings save, which says so
    while chats run (``busy``), or with ``interrupt`` the forced one."""
    if os.environ.get("HERMES_PLUGIN_HOST_PROCESS") == "1":
        return {"started": False, "busy": False, "error": _ROOM_SETUP_HOST_RESTART}
    gateway = _optional_module("hermes_cli.web_server_gateway")
    spawn = getattr(_optional_module("hermes_cli.web_server"), "_spawn_gateway_restart", None)
    try:
        if interrupt and callable(spawn):
            if _room_setup().takes_interrupt(spawn):
                spawn("default", interrupt=True)
            else:
                spawn("default")
            return {"started": True, "busy": False, "error": None}
        refusal_of = getattr(gateway, "live_session_refusal", None)
        refusal = refusal_of("default", "restart") if callable(refusal_of) else None
        if refusal:
            return {"started": False, "busy": True, "error": str(refusal)}
        after = getattr(gateway, "_restart_gateway_after", None)
        if callable(after):
            answer = after("default", what="Linked Rooms setup", label="Linked Rooms setup")
            return {"started": bool(answer.get("restart_started")), "busy": bool(answer.get("restart_busy")),
                    "error": answer.get("restart_error") or None}
        if callable(spawn):
            spawn("default")
            return {"started": True, "busy": False, "error": None}
    except Exception as exc:  # noqa: BLE001 - the save stays; the answer says the restart didn't start
        # Its text stays in the log: it can name this host's paths.
        logger.warning("Linked Rooms setup couldn't restart the gateway (%s)", type(exc).__name__, exc_info=True)
        return {"started": False, "busy": False, "error": _ROOM_SETUP_RESTART_FAILED}
    return {"started": False, "busy": False, "error": _ROOM_SETUP_CANT_RESTART}


_ROOM_SETUP_READ = object()  # GET's: a POST's body is read as JSON, and null is refused


def room_setup(profile: Optional[str], body: Any = _ROOM_SETUP_READ) -> Dict[str, Any]:
    """The setup state for ``profile`` (the dashboard's own without one),
    or with ``body`` the setup done, and the restart when it needs one."""
    module = _room_setup()
    home = Path(core._calls_home())
    name = (profile or "").strip()
    if not name or name.lower() == "current":
        name = module.profile_name(home)
    read = body is _ROOM_SETUP_READ
    if not read:
        if not isinstance(body, dict):
            raise core.TokenError(400, 'Expected {"ceiling": [...], "interrupt": false}', "bad_request")
        if not isinstance(body.get("interrupt", False), bool):
            raise core.TokenError(400, "interrupt is true or false", "bad_request")
    try:
        hermes = _room_setup_hermes(module)
        if read:
            return module.state(hermes, home, name)
        answer = module.apply(hermes, home, name, ceiling=body.get("ceiling"))
    except module.SetupError as exc:
        raise core.TokenError(exc.status, str(exc), exc.code)
    if "restart" in answer["needs"]:
        answer["restart"] = _restart_room_gateway(bool(body.get("interrupt")))
    return answer


async def _room_setup_route(profile: Optional[str], run: Callable[[], Dict[str, Any]],
                            limiter: "core._MintLimiter") -> Any:
    try:
        return {"ok": True, **(await core._run_scoped(profile, core._limited(limiter, None, run)))}
    except core.TokenError as exc:
        # ``code`` is room_setup's word for the refusal (bad_ceiling, yolo,
        # managed, standalone_profile, no_port, unsupported…).
        return phone_data._store_refusal(exc)
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise core._unexpected("setup", exc, feature="Linked Rooms")


@router.get("/rooms/setup")
async def get_room_setup(request: Request, response: Response, profile: Optional[str] = None) -> Any:
    """What setting this Hermes up for rooms would do (``needs``) and what
    blocks it (``blockers``), with the API server, the room ceiling and its
    choices, approvals and the profile's room policy."""
    response.headers["Cache-Control"] = "no-store"
    profile = await core._conduit_route_profile_no_store(request, profile)
    return await _room_setup_route(profile, lambda: room_setup(profile), _room_setup_read_limiter)


@router.post("/rooms/setup", dependencies=core._UNLESS_LOCKED)
async def post_room_setup(request: Request, response: Response, profile: Optional[str] = None) -> Any:
    """Sets this Hermes up for rooms, with ``ceiling`` (the saved one, or
    the default, without it), and restarts the gateway when the API server
    isn't running: ``restart.busy`` while chats run, which ``interrupt``
    overrides."""
    response.headers["Cache-Control"] = "no-store"
    profile = await core._conduit_route_profile_no_store(request, profile)
    body = await core._read_json_body(request, ROOM_SETUP_MAX_BODY_BYTES)
    return await _room_setup_route(profile, lambda: room_setup(profile, body), _room_setup_limiter)
