"""Starting a room over Links on its home, and changing its routes while it goes on, for Linked Rooms
(designs/cross-owner-agents, N5a and N5b).

Once everyone invited has answered a draft (room_invites.py), or its creator starts it anyway, the home creates
the room in Hermes' running room service with every agent as a member, and gives each agent from a linked Hermes
its route: the bridge's URL for the agent's Link and profile (room_bridge.url), with the grant its owner's Hermes
minted for its place. Before that, Hermes' own client probes each one through the bridge, as Hermes' own
groups.peer.register does, and an agent whose Hermes refuses its grant, or whose catalog or scope changed, is left
out.

Later, in the same service, a member's fresh grant goes into its route (``swap``), and a member that left or was
taken out is cut off (``cut_off``, ``revoke``), as N2's guard cuts off one whose Hermes no longer accepts the room.

The room service is the one the request that made the draft would have used: the canonical one (each profile's
session authority's, found from the gateway runner the dashboard app keeps) when the dashboard runs in the
gateway, else the legacy one (tui_gateway.methods_groups.get_hosted_room_service). A draft from the CLI starts in
the gateway's while a gateway's dashboard runs the rooms loop (it holds GATEWAY_LOCK), even with a dashboard
process of its own beside it, and else in the legacy one.

Hermes' calls come in through ``Hermes``. Nothing here imports Hermes at module level, so the tests give it a
fake, and a Hermes without what a start needs keeps its drafts and says why (``unsupported``).
"""

from __future__ import annotations

import dataclasses
import importlib
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from . import room_bridge, room_cutoff, room_identity, room_links

logger = logging.getLogger(__name__)

# What a probe says of an agent: its Hermes took the grant for the answer's catalog and scope, refused it (or its
# catalog or scope changed: left out, as groups.peer.register refuses), or didn't answer (kept: the bridge settles
# its turns, N4b).
OK, REFUSED, UNANSWERED = "ok", "refused", "unanswered"

# Who a room from the CLI belongs to in a canonical service: the dashboard's session-token owner, as Hermes names
# a request with no other identity (hermes_cli.web_server_sessions._session_mutation_context).
CLI_OWNER = {"user_id": "legacy-token-owner", "provider": "session-token"}

# Held by the rooms loop of a process whose dashboard runs in the gateway (Starter.claim), so a draft from the CLI
# starts in the gateway's room service and never in that of a dashboard process beside it (Desktop's own, say).
GATEWAY_LOCK = ".gateway-rooms.lock"

_gateway_locks: dict[str, Any] = {}
_gateway_locks_lock = threading.Lock()


class StartError(Exception):
    """Why a draft can't start here now: ``unsupported`` (this Hermes has no room service the plugin can use),
    ``elsewhere`` (its service runs in another process) or ``no_room_service`` (Hermes' isn't running)."""

    def __init__(self, code: str, message: str, status: int = 503) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclasses.dataclass(frozen=True)
class Found:
    """The running room service a draft starts in: ``kind`` legacy or canonical, and for a canonical one the
    subject that owns the room and the home whose session authority's it is."""

    kind: str
    service: Any
    subject: Optional[str]
    home: Optional[str] = None


class Starter:
    """The home's side of a start for the installation at ``root``: ``hermes`` is Hermes' calls (``Hermes``)."""

    def __init__(self, root: Any, *, hermes: Any = None, clock: Callable[[], float] = time.time) -> None:
        self.root = Path(root)
        self.hermes = hermes if hermes is not None else Hermes(self.root)
        self.clock = clock

    def supported(self) -> Optional[str]:
        """None when this Hermes has what a start needs, else why not (``unsupported``)."""
        return self.hermes.supported()

    def installation(self) -> str:
        """This installation's id, as Hermes' rooms name their home and authority."""
        return self.hermes.installation()

    def claim(self) -> None:
        """Holds this installation's drafts from the CLI (GATEWAY_LOCK) while this process's dashboard runs in the
        gateway, and lets them go once it doesn't: what the rooms loop does each time it looks."""
        _hold(self.root, self.hermes.in_gateway())

    def find(self, where: Mapping[str, Any]) -> Found:
        """The running room service ``where`` names (room_invites._where), or StartError."""
        if where["kind"] == "any" and not self.hermes.in_gateway() and _held_elsewhere(self.root):
            raise StartError("elsewhere", "The gateway, whose dashboard runs too, starts the rooms from the CLI")
        return self.hermes.find(where)

    def probe(self, found: Found, *, room_id: str, home_install_id: str, link_id: str,
              agent: Mapping[str, Any]) -> str:
        """What the agent's Hermes answers its grant with through the bridge, checked as Hermes'
        groups.peer.register checks a route's probe, for the scope the room will give it (authority epoch 1,
        this installation's authority): OK, REFUSED or UNANSWERED. BridgeError before the bridge listens."""
        client = self.hermes.client(self.hermes.link_url(room_bridge.url(self.root, link_id, agent["profile"])),
                                    found.service.db_path)
        try:
            answer = client.probe(grant=agent["grant"])
        except self.hermes.error as exc:
            status = getattr(exc, "status_code", None)
            # 4xx is its Hermes (or the bridge, for a Link that ended) refusing the grant; 408 and 429 aren't.
            return REFUSED if isinstance(status, int) and 400 <= status < 500 and status not in (408, 429) \
                else UNANSWERED
        except Exception as exc:  # noqa: BLE001 - one agent's probe never stops the start
            logger.info("Linked Rooms couldn't probe an agent over Link %s (%s)", link_id, type(exc).__name__)
            return UNANSWERED
        try:
            stored = self.hermes.catalog(agent["catalog"])
            live = self.hermes.catalog(answer.get("catalog"))
        except Exception:  # noqa: BLE001 - a catalog this Hermes can't read is one it can't route to
            return REFUSED
        expected = {"room_id": room_id, "home_install_id": home_install_id, "authority_gateway_id": home_install_id,
                    "member_id": agent["member_id"], "target_profile": agent["profile"]}
        if live != stored or self.hermes.protocol_version not in stored.protocol_versions \
                or "direct" not in stored.link_modes or any(answer.get(name) != value
                                                            for name, value in expected.items()) \
                or answer.get("authority_epoch") != 1:
            return REFUSED
        return OK

    def create(self, found: Found, *, room_id: str, name: str, members: list[dict[str, Any]]) -> None:
        """The room, with every member, in the service: a canonical room is the subject's, claimed in the same
        transaction (session_hosted_service's authorize_room)."""
        admit = None
        if found.kind == "canonical":
            service, subject = found.service, found.subject

            def admit(conn: Any) -> Any:
                return service.authorize_room(subject, room_id, create=True, conn=conn)
        found.service.create_room(room_id=room_id, name=name, members=members, admit=admit)

    def register(self, found: Found, *, room_id: str, home_install_id: str, link_id: str,
                 agent: Mapping[str, Any]) -> None:
        """The agent's route, live in the running service at the bridge's URL for its Link and profile, as
        groups.peer.register makes it."""
        target_url = self.hermes.link_url(room_bridge.url(self.root, link_id, agent["profile"]))
        catalog = self.hermes.catalog(agent["catalog"])
        route = self.hermes.route(
            home_install_id=home_install_id, member_id=agent["member_id"],
            target_install_id=catalog.installation_id, target_profile=agent["profile"],
            capability_digest=catalog.catalog_digest,
            execution_policy_digest=catalog.execution_policy.policy_digest,
            cancellation_scope_id=f"cancel-{room_id}", trace_id=f"trace-{os.urandom(16).hex()}",
            grant=agent["grant"])
        found.service.register_peer_route(room_id=room_id, member_id=agent["member_id"], route=route,
                                          client=self.hermes.client(target_url, found.service.db_path),
                                          target_url=target_url, catalog=catalog)

    def swap(self, found: Found, *, room_id: str, home_install_id: str, link_id: str,
             agent: Mapping[str, Any], registered: bool) -> bool:
        """The agent's fresh grant in its route: register_peer_route again with the route as it is but the
        grant (its trace and cancellation scope kept), which saves the stored link before it publishes, the way
        Hermes' own refresh does. A member cut off at the start, whose route the service hasn't got, is
        registered as at the start; a route that was ``registered`` and that the service hasn't got any more,
        it dropped itself (Hermes' disband revokes a room's routes before room_cleanup's guard ends the room
        here), and isn't made again, as Hermes' own refresh doesn't. False when nothing changed: that, or the
        route's grant as new already (Hermes refreshed it itself since)."""
        key = (room_id, agent["member_id"])
        route = getattr(found.service, "peer_routes", {}).get(key)
        if route is None and registered:
            logger.info("Linked Rooms left member %s in room %s without its fresh grant: the room service dropped "
                        "its route", agent["member_id"], room_id)
            return False
        if route is None:
            self.register(found, room_id=room_id, home_install_id=home_install_id, link_id=link_id, agent=agent)
            return True
        issued, current = room_bridge.grant_issued_at(agent["grant"]), room_bridge.grant_issued_at(route.grant)
        if issued is None or (current is not None and current >= issued):
            return False
        target_url = self.hermes.link_url(room_bridge.url(self.root, link_id, agent["profile"]))
        client = getattr(found.service, "peer_clients", {}).get(key) \
            or self.hermes.client(target_url, found.service.db_path)
        found.service.register_peer_route(room_id=room_id, member_id=agent["member_id"],
                                          route=dataclasses.replace(route, grant=agent["grant"]), client=client,
                                          target_url=target_url, catalog=self.hermes.catalog(agent["catalog"]))
        return True

    def cut_off(self, found: Found, *, room_id: str, member_id: str) -> None:
        """The member's route marked as needing reauthorization (room_cutoff.CUT_OFF), where the service keeps
        it and in its stored link, which N2's guard reads before each of the member's queued turns: they fail
        before they're sent."""
        mark = getattr(found.service, "_set_route_status", None)
        if callable(mark):
            mark(room_id, member_id, room_cutoff.CUT_OFF)
        else:
            self.hermes.mark(found.service.db_path, room_id=room_id, member_id=member_id, status=room_cutoff.CUT_OFF)

    def revoke(self, found: Found, *, room_id: str, member_id: str) -> None:
        """A member taken out of the room: its grant revoked on its Hermes through its route's client, as
        Hermes' disband revokes each route's, when that answers, and its route cut off either way."""
        key = (room_id, member_id)
        route = getattr(found.service, "peer_routes", {}).get(key)
        revoke = getattr(getattr(found.service, "peer_clients", {}).get(key), "revoke_grant", None)
        if route is not None and callable(revoke):
            try:
                revoke(grant=route.grant)
            except Exception as exc:  # noqa: BLE001 - its Hermes is told by room.ended too
                logger.info("Linked Rooms couldn't revoke member %s's grant in room %s on its Hermes (%s); it's cut "
                            "off", member_id, room_id, type(exc).__name__)
        self.cut_off(found, room_id=room_id, member_id=member_id)


def where_from(request: Any, profile: Optional[str]) -> dict[str, Any]:
    """Where a draft a dashboard request makes starts (R13): with the dashboard in the gateway (its app's session
    authority set), in the canonical service of the request's profile, the room owned by the request's subject
    as Conduit's own rooms there are (hermes_cli.web_server_sessions._session_mutation_context, whose refusals
    stand); else in the legacy one."""
    if getattr(request.app.state, "session_authority", None) is None:
        return {"kind": "legacy"}
    try:
        context = importlib.import_module("hermes_cli.web_server_sessions")._session_mutation_context
    except (ImportError, AttributeError):
        logger.warning("Linked Rooms can't make rooms with linked people where the dashboard runs in the gateway: "
                       "this Hermes has no hermes_cli.web_server_sessions._session_mutation_context")
        raise StartError("unsupported", "This Hermes can't host rooms with linked people; update Hermes", 501) \
            from None
    authority, principal = context(request, profile)
    return {"kind": "canonical", "home": str(Path(authority.db.db_path).parent), "subject": str(principal.subject)}


class Hermes:
    """This Hermes' calls for a start, as groups.peer.register makes them (tui_gateway/methods_groups.py), and its
    running room services: the legacy one this process runs (tui_gateway.methods_groups'
    get_hosted_room_service) and the canonical ones, each served profile's session authority's, found from the
    gateway runner the dashboard app keeps (hermes_cli.web_server's app.state.gateway_runner,
    gateway.session_authorities.authority_for_home). A Hermes without RoomLink's calls, or without either
    lookup, can't start rooms with linked people (``unsupported``)."""

    _ROOMLINK = (("gateway.hosted_room_peer", ("GatewayRoomCatalog", "PROTOCOL_VERSION", "validate_room_link_url")),
                 ("gateway.hosted_rooms", ("local_authority_gateway_id",)),
                 ("tui_gateway.hosted_room_peer_http", ("PeerRunsHTTPClient", "PeerRunsHTTPError")),
                 ("tui_gateway.hosted_room_peer_transport", ("PeerMemberRoute",)))
    _LEGACY = (("tui_gateway.methods_groups", ("get_hosted_room_service",)),)
    _CANONICAL = (("gateway.session_authorities", ("authority_for_home",)),
                  ("gateway.session_identity", ("authenticated_subject",)))

    def __init__(self, root: Any) -> None:
        self.root = Path(root)
        self._calls: Optional[dict[str, Any]] = None

    def supported(self) -> Optional[str]:
        return None if self._lacking() is None else "unsupported"

    def in_gateway(self) -> bool:
        """Whether this process's dashboard runs in the gateway, whose services a draft from the CLI uses."""
        return self._load()["authority_for_home"] is not None and self._runner() is not None

    def installation(self) -> str:
        return self._call("local_authority_gateway_id")()

    def find(self, where: Mapping[str, Any]) -> Found:
        lacking = self._lacking()
        if lacking is not None:
            raise StartError("unsupported", f"This Hermes has no {lacking}", 501)
        calls = self._load()
        kind = where["kind"]
        runner = self._runner() if calls["authority_for_home"] is not None else None
        if kind == "canonical" or (kind == "any" and runner is not None):
            if calls["authority_for_home"] is None:
                raise StartError("unsupported", "This Hermes has no gateway.session_authorities.authority_for_home",
                                 501)
            if runner is None:
                raise StartError("elsewhere", "The room's service runs in the gateway, which this process isn't")
            home = Path(where["home"]) if kind == "canonical" else self.root
            authority = calls["authority_for_home"](runner, home)
            service = getattr(authority, "hosted_room_service", None) if authority is not None else None
            if not _running(service):
                raise StartError("no_room_service", "The gateway's room service for that profile isn't running")
            subject = where["subject"] if kind == "canonical" else calls["authenticated_subject"](dict(CLI_OWNER))
            return Found("canonical", service, subject, str(home))
        if calls["get_hosted_room_service"] is None:
            raise StartError("unsupported", "This Hermes has no tui_gateway.methods_groups.get_hosted_room_service",
                             501)
        service = calls["get_hosted_room_service"]()
        if service is None:
            raise StartError("no_room_service", "Hermes' room service isn't running in this process")
        return Found("legacy", service, None)

    def catalog(self, value: Any) -> Any:
        return self._call("GatewayRoomCatalog").from_mapping(value)

    def link_url(self, value: str) -> str:
        return self._call("validate_room_link_url")(value)[0]

    def client(self, base_url: str, receipt_db_path: Any) -> Any:
        return self._call("PeerRunsHTTPClient")(base_url=base_url, api_key="", receipt_db_path=receipt_db_path)

    def route(self, **fields: Any) -> Any:
        return self._call("PeerMemberRoute")(**fields)

    def mark(self, db_path: Any, *, room_id: str, member_id: str, status: str) -> None:
        """A stored route's status, for a service that doesn't keep its routes' itself."""
        importlib.import_module("gateway.hosted_room_links").mark_room_link_status(
            db_path, room_id=room_id, member_id=member_id, status=status)

    @property
    def error(self) -> type:
        return self._call("PeerRunsHTTPError")

    @property
    def protocol_version(self) -> int:
        return self._call("PROTOCOL_VERSION")

    def _call(self, name: str) -> Any:
        value = self._load()[name]
        if value is None:
            raise StartError("unsupported", f"This Hermes has no {name}", 501)
        return value

    def _lacking(self) -> Optional[str]:
        """What this Hermes lacks of what a start needs (RoomLink's calls, and one of the lookups), or None."""
        calls = self._load()
        missing = [f"{module_name}.{name}" for module_name, names in self._ROOMLINK for name in names
                   if calls[name] is None]
        if not missing and calls["get_hosted_room_service"] is None and calls["authority_for_home"] is None:
            missing = ["tui_gateway.methods_groups.get_hosted_room_service",
                       "gateway.session_authorities.authority_for_home"]
        return ", ".join(missing) or None

    def _load(self) -> dict[str, Any]:
        if self._calls is None:
            calls: dict[str, Any] = {}
            for module_name, names in (*self._ROOMLINK, *self._LEGACY, *self._CANONICAL):
                try:
                    module = importlib.import_module(module_name)
                except ImportError:
                    module = None
                calls.update((name, getattr(module, name, None)) for name in names)
            if calls["authenticated_subject"] is None:
                calls["authority_for_home"] = None  # a canonical lookup without the CLI's owner is none
            self._calls = calls
        return self._calls

    @staticmethod
    def _runner() -> Any:
        """The gateway runner the dashboard app keeps when it runs in the gateway (gateway/run_api.py), or None."""
        app = getattr(sys.modules.get("hermes_cli.web_server"), "app", None)
        return getattr(getattr(app, "state", None), "gateway_runner", None)


def _hold(root: Path, holding: bool) -> None:
    key = str(root)
    with _gateway_locks_lock:
        handle = _gateway_locks.get(key)
        if holding and handle is None:
            directory = root / room_identity.DIRECTORY
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            handle = open(directory / GATEWAY_LOCK, "a+b")
            if room_links._try_lock(handle):
                _gateway_locks[key] = handle
            else:
                handle.close()  # another gateway's, for the same installation
        elif not holding and handle is not None:
            del _gateway_locks[key]
            room_links._unlock(handle)
            handle.close()


def _held_elsewhere(root: Path) -> bool:
    """Whether a gateway's rooms loop, in another process, holds this installation's drafts from the CLI."""
    with _gateway_locks_lock:
        if str(root) in _gateway_locks:
            return False
    try:
        handle = open(root / room_identity.DIRECTORY / GATEWAY_LOCK, "rb")
    except OSError:
        return False
    with handle:
        if not room_links._try_lock(handle):
            return True
        room_links._unlock(handle)
    return False


def _running(service: Any) -> bool:
    if service is None:
        return False
    try:
        status = service.runtime.status()
    except Exception:  # noqa: BLE001 - a service that can't say is one that isn't running
        return False
    return bool(status.get("running")) and not status.get("stopping")
