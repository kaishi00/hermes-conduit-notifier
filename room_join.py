"""Joining a room over a Link with this installation's agents, for Linked Rooms (designs/cross-owner-agents, N5a and
N5b).

When a person here joins a room another Hermes hosts (room_invites.py), each agent they bring gets a grant from
this Hermes for its place in the room: dispatch, status and stop (never approve: approvals stay on the member's
own Hermes, decision 6), for 24 hours to start turns and 30 days to read and stop them, the most Hermes allows. A
look-only grant first reads which execution policy this Hermes' own API server runs for the profile, which the
grant has to name (that process can see another policy than this one reads from the profile's config). The
reservation holds the place, the API server answers with the agent's catalog for the grant, and the bridge's member
side takes the Link's requests for it (room_bridge_member.bind). Leaving undoes all three. While the room goes on, a
fresh grant for the same place replaces the agent's every 12 hours (``renew``), so it never runs out.

Hermes' calls come in through ``Hermes``. Nothing here imports Hermes at module level, so the tests give it a
fake.
"""

from __future__ import annotations

import importlib
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from . import room_bridge, room_bridge_member

logger = logging.getLogger(__name__)

# Hermes' MAX_DISPATCH_GRANT_TTL_SECONDS and MAX_STATUS_GRANT_TTL_SECONDS.
DISPATCH_S = 24 * 3600
STATUS_S = 30 * 86400
# Never approve (decision 6).
PERMISSIONS = ("dispatch", "status", "stop")
# The look-only grant that reads the API server's policy before the agent's grant is minted. Hermes revokes a grant's
# whole scope, the agent's grant with it, so it isn't revoked once the join is made: it never leaves this process,
# and runs out in a minute.
PROBE_S = 60
# A turn on Codex's app server runs its tools outside Hermes' tool hooks, where a room's rules can't see them.
CODEX = "codex_app_server"
CODEX_SETTINGS = ("openai_runtime", "api_mode")
# Hermes' API server's live catalog for a room grant (api_server_room_grants.py).
CAPABILITIES_PATH = "/v1/room-members/capabilities"

_DIGEST = re.compile(r"[0-9a-f]{64}")


class JoinError(Exception):
    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class Joiner:
    """The member's side of a join for the installation at ``root``: ``hermes`` is Hermes' calls (``Hermes``)."""

    def __init__(self, root: Any, *, hermes: Any = None, clock: Callable[[], float] = time.time) -> None:
        self.root = Path(root)
        self.hermes = hermes if hermes is not None else Hermes(self.root)
        self.clock = clock

    def check(self, profile: str, *, joining: bool = True) -> None:
        """Refuses (JoinError) a profile that can't take part in a room with other people's agents: one whose
        turns run on Codex's app server (``codex``), or, to join a room another Hermes hosts, one not set up for
        rooms (``not_set_up``, room_setup.py). The home's own agents need no setup: they run in its room service,
        with no grant."""
        config = self.hermes.config(profile)
        model = config.get("model") if isinstance(config, Mapping) else None
        if isinstance(model, Mapping) and any(str(model.get(name) or "").strip().lower() == CODEX
                                              for name in CODEX_SETTINGS):
            raise JoinError("codex", f"Profile {profile} runs its turns on Codex's app server, whose tools run "
                                     "outside Hermes' tool hooks, so it can't be in a room with other people's agents")
        if joining and not self.hermes.ready(profile):
            raise JoinError("not_set_up", f"Profile {profile} isn't set up for rooms with other people's agents: "
                                          "turn Linked Rooms on for it first")

    def join(self, link_id: str, *, room_id: str, home_install_id: str, authority_gateway_id: str,
             authority_epoch: int, member_id: str, profile: str) -> dict[str, Any]:
        """``profile``'s place ``member_id`` in the room the Link ``link_id``'s Hermes hosts: its grant, its
        catalog and when the grant's status runs out. JoinError ``api_server`` (503) when this Hermes' API server
        doesn't answer for it, with nothing left behind."""
        now = int(self.clock())
        installation = self.hermes.installation()
        scope = {"home_install_id": home_install_id, "room_id": room_id, "member_id": member_id,
                 "target_profile": profile}
        expected = {**scope, "authority_gateway_id": authority_gateway_id, "authority_epoch": authority_epoch}
        place = {**expected, "target_install_id": installation}
        minted: list[str] = []
        try:
            probe, _ = self._mint(minted, place, self.hermes.policy_digest(profile), ("status",), now, PROBE_S,
                                  PROBE_S)
            digest = self._catalog(profile, probe, installation, expected)["execution_policy"]["policy_digest"]
            grant, claims = self._mint(minted, place, digest, PERMISSIONS, now, DISPATCH_S, STATUS_S)
            catalog = self._catalog(profile, grant, installation, expected)
            if catalog["execution_policy"]["policy_digest"] != digest:
                raise JoinError("api_server", f"This Hermes' API server changed profile {profile}'s room policy "
                                              "while it joined; try again", 503)
            room_bridge_member.bind(self.root, link_id, scope)
        except BaseException:
            if minted:
                self._revoke(minted[-1])  # the whole scope, until its last grant runs out
            raise
        return {"grant": grant, "catalog": catalog, "status_expires_at": int(claims["status_expires_at"])}

    def renew(self, grant: str) -> dict[str, Any]:
        """A fresh grant for the place ``grant`` holds (N5b): the same place and policy, for 24 hours to start
        turns and 30 days to read and stop them from now, minted once this Hermes' API server still takes
        ``grant`` (live, not revoked, its reservation current) and still runs the policy it names, and
        reserved. Never revokes anything: Hermes revokes a grant's whole scope, ``grant`` with it. JoinError
        ``gone`` (410) for a grant past its last day, ``refused`` (409) when the API server refuses it,
        ``policy_changed`` (409) when it runs another policy for the profile now, ``api_server`` (503) when it
        doesn't answer."""
        try:
            claims = self.hermes.decode(grant, "status")
        except ValueError:  # Hermes' HostedRoomGrantError: past its horizon, or not this Hermes' any more
            raise JoinError("gone", "This agent's grant for the room ran out", 410) from None
        place = {name: claims[name] for name in ("room_id", "home_install_id", "authority_gateway_id",
                                                 "authority_epoch", "member_id", "target_install_id",
                                                 "target_profile")}
        profile, digest = place["target_profile"], claims["execution_policy_digest"]
        expected = {name: value for name, value in place.items() if name != "target_install_id"}
        catalog = self._catalog(profile, grant, place["target_install_id"], expected, renewing=True)
        if catalog["execution_policy"]["policy_digest"] != digest:
            raise JoinError("policy_changed", f"Profile {profile}'s room policy changed since it joined", 409)
        fresh, fresh_claims = self._mint([], place, digest, PERMISSIONS, int(self.clock()), DISPATCH_S, STATUS_S)
        return {"grant": fresh, "status_expires_at": int(fresh_claims["status_expires_at"]),
                "issued_at": int(fresh_claims["issued_at"])}

    def leave(self, link_id: str, grant: str) -> None:
        """Undoes a join: the Link no longer carries the grant's requests, and the grant's scope is revoked."""
        scope = room_bridge.grant_scope(grant)
        if scope is not None:
            room_bridge_member.unbind(self.root, link_id, scope)
        self._revoke(grant)

    def _mint(self, minted: list[str], place: Mapping[str, Any], digest: str, permissions: tuple[str, ...], now: int,
              ttl: int, status_ttl: int) -> tuple[str, dict[str, Any]]:
        """A grant for the place and its claims, reserved. ``minted`` keeps it from the moment it's made, for a join
        that fails to revoke."""
        grant = self.hermes.issue(grant_id=f"grant-{uuid.uuid4().hex}", **place, execution_policy_digest=digest,
                                  permissions=permissions, issued_at=now, ttl_seconds=ttl,
                                  status_ttl_seconds=status_ttl)
        minted.append(grant)
        claims = self.hermes.decode(grant, "status")
        self.hermes.reserve(claims, claims["status_expires_at"])
        return grant, claims

    def _catalog(self, profile: str, grant: str, installation: str, scope: Mapping[str, Any], *,
                 renewing: bool = False) -> dict[str, Any]:
        """The agent's catalog, as this Hermes' API server answers for the grant (what the home's probe will
        compare at the start). A grant it refuses as it renews (4xx, as the home's probe reads one) is
        ``refused``: never renewed again."""
        status, answer = self.hermes.capabilities(profile, grant)
        if status is None:
            raise JoinError("api_server", f"This Hermes' API server didn't answer for profile {profile}; check that "
                                          "Linked Rooms is on for it and the gateway runs", 503)
        if status != 200:
            code = answer.get("error", {}).get("code") if isinstance(answer.get("error"), dict) else None
            if renewing and 400 <= status < 500 and status not in (408, 429):
                raise JoinError("refused", f"This Hermes' API server refused profile {profile}'s room grant "
                                           f"({code or status})", 409)
            raise JoinError("api_server", f"This Hermes' API server refused profile {profile}'s room grant "
                                          f"({code or status})", 503)
        catalog = answer.get("catalog")
        if any(answer.get(name) != value for name, value in scope.items()) or not isinstance(catalog, dict) \
                or catalog.get("installation_id") != installation or not isinstance(
                    catalog.get("execution_policy"), dict) or not isinstance(
                    catalog["execution_policy"].get("policy_digest"), str) or not _DIGEST.fullmatch(
                    catalog["execution_policy"]["policy_digest"]):
            raise JoinError("api_server", f"This Hermes' API server answered for profile {profile} with a catalog "
                                          "the room can't use", 503)
        return catalog

    def _revoke(self, grant: str) -> None:
        try:
            claims = self.hermes.decode(grant, "status")
        except Exception:  # noqa: BLE001 - a grant past its horizon has no place left to revoke
            return
        self.hermes.revoke(claims, claims["status_expires_at"])


class Hermes:
    """This Hermes' calls for a join, for the installation at ``root``: the grants, as its API server's room
    member invitation mints them (gateway.hosted_room_peer and gateway.hosted_rooms, with the installation's own
    secret and shared-state db), each profile's room setup and policy (room_setup.py; ``setup``, loaded when
    first needed), and its API server, reached as the bridge's member side reaches it (``api_url(profile)``, its
    address for a profile). A Hermes without one of them refuses joins (``unsupported``, 501), naming what it
    lacks in the log once."""

    def __init__(self, root: Path, *, setup: Any = None, api_url: Optional[Callable[[str], str]] = None) -> None:
        self.root = Path(root)
        self._setup = setup
        self._api = room_bridge_member.Member(self.root, send=lambda *args, **kwargs: None,
                                              api_url=api_url or self._api_url)
        self._told: set[str] = set()

    def config(self, profile: str) -> Mapping[str, Any]:
        """The profile's config.yaml as Hermes reads it (its managed settings and env references in)."""
        load = self._need("hermes_cli.config_effective", "load_user_config_effective")
        return load(self._home(profile) / "config.yaml")

    def ready(self, profile: str) -> bool:
        from . import room_setup

        return bool(room_setup.state(self._setup_calls(), self._home(profile), profile)["ready"])

    def policy_digest(self, profile: str) -> str:
        return self._setup_calls().policy(profile, self._home(profile))["policy_digest"]

    def installation(self) -> str:
        return self._need("gateway.hosted_rooms", "local_authority_gateway_id")()

    def issue(self, **fields: Any) -> str:
        issue, secret = self._need("gateway.hosted_room_peer", "issue_room_grant", "gateway_room_grant_secret")
        return issue(secret(self.root), **fields)

    def decode(self, grant: str, permission: str) -> dict[str, Any]:
        decode, secret = self._need("gateway.hosted_room_peer", "decode_room_grant", "gateway_room_grant_secret")
        return decode(secret(self.root), grant, permission=permission)

    def reserve(self, claims: Mapping[str, Any], expires_at: float) -> None:
        reserve, db_path = self._need("gateway.hosted_rooms", "reserve_peer_room", "default_db_path")
        reserve(db_path(), claims=claims, expires_at=expires_at)

    def revoke(self, claims: Mapping[str, Any], expires_at: float) -> None:
        revoke, db_path = self._need("gateway.hosted_rooms", "revoke_room_grant_scope", "default_db_path")
        revoke(db_path(), claims=claims, expires_at=expires_at)

    def capabilities(self, profile: str, grant: str) -> tuple[Optional[int], dict]:
        """What this Hermes' API server answers its room capabilities route with for the grant: its status and
        JSON object, or None and {} when it doesn't answer."""
        return self._api._call(profile, "GET", CAPABILITIES_PATH, grant)

    def _setup_calls(self) -> Any:
        if self._setup is None:
            from . import room_setup

            try:
                self._setup = room_setup.Hermes.load()
            except room_setup.SetupError as exc:
                raise JoinError(exc.code, str(exc), exc.status) from None
        return self._setup

    def _api_url(self, profile: str) -> str:
        from . import room_setup

        return room_setup.local_api_url(self._setup_calls(), self.root, profile)

    def _home(self, profile: str) -> Path:
        # Where Hermes keeps a profile (hermes_cli.profiles.get_profile_dir).
        return self.root if profile == "default" else self.root / "profiles" / profile

    def _need(self, module_name: str, *names: str) -> Any:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            module = None
        found = [getattr(module, name, None) for name in names]
        missing = ", ".join(f"{module_name}.{name}" for name, value in zip(names, found) if not callable(value))
        if missing:
            if missing not in self._told:
                self._told.add(missing)
                logger.warning("Linked Rooms can't join rooms over Links on this Hermes: it has no %s", missing)
            raise JoinError("unsupported", "This Hermes is missing what joining a room over a Link needs; update "
                                           "Hermes", 501)
        return found[0] if len(found) == 1 else found
