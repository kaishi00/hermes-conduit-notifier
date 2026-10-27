"""Joining a room over a Link with this Hermes' own calls (designs/cross-owner-agents, N5a): room_join.Hermes, the
grants as Hermes' API server mints them for a room member, the profile's room setup, and its API server's catalog
for the grant.

Hermes' grant functions are fakes of c73cbcc4's gateway.hosted_room_peer and gateway.hosted_rooms (9e01b44 and
930e5ed have the same), with their checks; its room setup is room_setup's tests' fake, and its API server the
bridge's tests' fake.
"""
import base64
import hashlib
import hmac
import json
import logging
import math
import sys
import types

import pytest

from test_room_bridge_member import NOW, ROOT, FakeHermes as ApiServer, _load, bridge
from test_room_setup import KEY, FakeHermes as SetupHermes, _apply

setups = sys.modules["conduit_push.room_setup"]

join_module = _load("conduit_push.room_join", ROOT / "room_join.py")

INSTALLATION = "install:laifu"
SCOPE = {"room_id": "conduit-zoo", "home_install_id": "install:furina", "authority_gateway_id": "install:furina",
         "authority_epoch": 1, "member_id": "l0123456789ab-1"}


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _claims(grant):
    encoded = grant.partition(".")[0]
    return json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))


class Grants:
    """gateway.hosted_room_peer's grants (issue_room_grant, decode_room_grant, gateway_room_grant_secret) and
    gateway.hosted_rooms' reservations (reserve_peer_room, revoke_room_grant_scope, default_db_path) and
    installation id, as c73cbcc4 has them."""

    def __init__(self, root):
        self.root = root
        self.secrets, self.reserved, self.revoked = [], [], []

    def gateway_room_grant_secret(self, root=None):
        self.secrets.append(root)
        return hashlib.sha256(str(root).encode()).digest()

    def issue_room_grant(self, secret, *, grant_id, room_id, home_install_id, authority_gateway_id, authority_epoch,
                         member_id, target_install_id, target_profile, execution_policy_digest=None,
                         permissions=("approve", "dispatch", "status", "stop"), issued_at=None, ttl_seconds=3600,
                         status_ttl_seconds=None, status_expires_at=None):
        now = NOW if issued_at is None else float(issued_at)
        horizon = now + float(ttl_seconds if status_ttl_seconds is None else status_ttl_seconds) \
            if status_expires_at is None else float(status_expires_at)
        if ttl_seconds <= 0 or ttl_seconds > 86400 or horizon < now + ttl_seconds or horizon > now + 30 * 86400:
            raise ValueError("room grant lifetime is invalid")
        if not set(permissions) <= {"approve", "dispatch", "status", "stop"}:
            raise ValueError("room grant permissions are invalid")
        assert execution_policy_digest is not None  # else Hermes computes it in this process, outside the API server
        payload = {"version": 2, "grant_id": grant_id, "room_id": room_id, "home_install_id": home_install_id,
                   "authority_gateway_id": authority_gateway_id, "authority_epoch": authority_epoch,
                   "member_id": member_id, "target_install_id": target_install_id, "target_profile": target_profile,
                   "execution_policy_digest": execution_policy_digest, "permissions": sorted(set(permissions)),
                   "issued_at": now, "expires_at": now + float(ttl_seconds), "status_expires_at": horizon}
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return f"{_b64(encoded)}.{_b64(hmac.new(secret, encoded, hashlib.sha256).digest())}"

    def decode_room_grant(self, secret, token, *, permission, now=None):
        encoded, _, signature = token.partition(".")
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        if _b64(hmac.new(secret, payload, hashlib.sha256).digest()) != signature:
            raise ValueError("room grant signature is invalid")
        claims = json.loads(payload)
        if permission not in claims["permissions"]:
            raise ValueError("room grant does not allow this operation")
        return claims

    def default_db_path(self):
        return self.root / "shared-state.db"

    def reserve_peer_room(self, db_path, *, claims, expires_at):
        assert math.isfinite(expires_at)
        self.reserved.append((db_path, claims["grant_id"], expires_at))

    def revoke_room_grant_scope(self, db_path, *, claims, expires_at):
        self.revoked.append((db_path, claims["grant_id"], expires_at))

    def local_authority_gateway_id(self):
        return INSTALLATION

    def install(self, monkeypatch):
        """Puts these in sys.modules, where room_join imports them from."""
        peer = types.ModuleType("gateway.hosted_room_peer")
        rooms = types.ModuleType("gateway.hosted_rooms")
        for name in ("gateway_room_grant_secret", "issue_room_grant", "decode_room_grant"):
            setattr(peer, name, getattr(self, name))
        for name in ("default_db_path", "reserve_peer_room", "revoke_room_grant_scope", "local_authority_gateway_id"):
            setattr(rooms, name, getattr(self, name))
        monkeypatch.setitem(sys.modules, "gateway.hosted_room_peer", peer)
        monkeypatch.setitem(sys.modules, "gateway.hosted_rooms", rooms)


@pytest.fixture
def laifu(tmp_path, monkeypatch):
    """Laifu's installation, with its default profile set up for rooms (its API server running) and a work
    profile that isn't."""
    root = tmp_path / "hermes"
    (root / "profiles" / "work").mkdir(parents=True)
    (root / "profiles" / "work" / "config.yaml").write_text("model: {}\n")
    setup = SetupHermes(root)
    monkeypatch.setattr(setups, "_answers", lambda host, port: False)
    monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)
    (root / ".env").write_text(f"API_SERVER_KEY={KEY}\n", encoding="utf-8")
    setup.serve()
    _apply(setup)
    grants = Grants(root)
    grants.install(monkeypatch)
    # hermes_cli.config_effective.load_user_config_effective, over what each config.yaml reads as.
    configs = {}
    effective = types.ModuleType("hermes_cli.config_effective")
    effective.load_user_config_effective = lambda path=None, **kwargs: dict(configs.get(path, {}))
    monkeypatch.setitem(sys.modules, "hermes_cli.config_effective", effective)
    api = ApiServer()
    side = types.SimpleNamespace(root=root, setup=setup, grants=grants, configs=configs, api=api)
    side.joiner = _joiner(side)
    yield side
    api.close()


def _joiner(side, api_url=None):
    hermes = join_module.Hermes(side.root, setup=side.setup.adapter(), api_url=api_url or side.api.url)
    return join_module.Joiner(side.root, hermes=hermes, clock=lambda: NOW)


def _capabilities(side, profile="default"):
    """What Laifu's API server answers its capabilities route with for the join's grant: the profile's catalog,
    with the policy the API server runs for it."""
    policy = side.setup.adapter().policy(profile, side.setup.home(profile))
    value = {"installation_id": INSTALLATION, "protocol_versions": [2], "link_modes": ["direct"],
             "persistent_process": True, "text": True, "attachments": False, "execution_policy": policy,
             "catalog_digest": "c" * 64}
    side.api.answers[("GET", "/v1/room-members/capabilities")] = (200, {
        "object": "hermes.room_member.capabilities", **SCOPE, "target_profile": profile, "catalog": value})
    return value


def _join(joiner, profile="default"):
    return joiner.join("link1", room_id=SCOPE["room_id"], home_install_id=SCOPE["home_install_id"],
                       authority_gateway_id=SCOPE["authority_gateway_id"], authority_epoch=SCOPE["authority_epoch"],
                       member_id=SCOPE["member_id"], profile=profile)


def _bound(side):
    return bridge.Store(side.root).read()["member"]["bindings"].get("link1", [])


def test_a_join_mints_with_the_installations_secret_and_holds_the_place_in_its_shared_state(laifu):
    catalog = _capabilities(laifu)
    joined = _join(laifu.joiner)

    claims = _claims(joined["grant"])
    assert claims["permissions"] == ["dispatch", "status", "stop"]
    assert (claims["target_install_id"], claims["target_profile"]) == (INSTALLATION, "default")
    assert claims["expires_at"] - claims["issued_at"] == 24 * 3600
    assert claims["status_expires_at"] - claims["issued_at"] == 30 * 86400
    assert claims["execution_policy_digest"] == catalog["execution_policy"]["policy_digest"]
    # The installation's secret, from its root, and its root's shared-state db.
    assert laifu.grants.secrets and set(laifu.grants.secrets) == {laifu.root}
    assert laifu.grants.reserved == [(laifu.root / "shared-state.db", claims["grant_id"], claims["status_expires_at"])]
    assert joined["catalog"] == catalog and joined["status_expires_at"] == int(claims["status_expires_at"])
    # The catalog comes from this Hermes' API server, for the grant, at the profile's address.
    (call,) = laifu.api.calls_to("GET", "/v1/room-members/capabilities")
    assert call["authorization"] == f"HermesRoom {joined['grant']}" and call["profile"] == "default"
    assert _bound(laifu) == [{name: claims[name] for name in ("home_install_id", "room_id", "member_id",
                                                             "target_profile")}]


def test_a_named_profile_joins_with_its_own_policy_at_its_own_address(laifu):
    (laifu.root / "profiles" / "work" / ".env").write_text(f"API_SERVER_KEY={'w' * 64}\n", encoding="utf-8")
    _apply(laifu.setup, "work")
    catalog = _capabilities(laifu, "work")
    joined = _join(laifu.joiner, "work")

    assert _claims(joined["grant"])["execution_policy_digest"] == catalog["execution_policy"]["policy_digest"]
    (call,) = laifu.api.calls_to("GET", "/v1/room-members/capabilities")
    assert call["profile"] == "work"


def test_leaving_revokes_the_grants_scope_in_the_shared_state(laifu):
    _capabilities(laifu)
    joined = _join(laifu.joiner)
    laifu.joiner.leave("link1", joined["grant"])

    claims = _claims(joined["grant"])
    assert laifu.grants.revoked == [(laifu.root / "shared-state.db", claims["grant_id"], claims["status_expires_at"])]
    assert _bound(laifu) == []


def test_a_profile_set_up_for_rooms_can_join_and_one_that_isnt_is_refused(laifu):
    laifu.joiner.check("default")
    with pytest.raises(join_module.JoinError) as refused:
        laifu.joiner.check("work")
    assert refused.value.code == "not_set_up"


@pytest.mark.parametrize("model", [{"openai_runtime": "codex_app_server"}, {"api_mode": "Codex_App_Server "}])
def test_a_codex_app_server_profile_is_refused_from_its_effective_config(laifu, model):
    laifu.configs[laifu.root / "config.yaml"] = {"model": model}
    with pytest.raises(join_module.JoinError) as refused:
        laifu.joiner.check("default")
    assert refused.value.code == "codex"


def test_a_hermes_without_room_grants_refuses_joins_naming_what_it_lacks_once(laifu, monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "gateway.hosted_room_peer", None)
    joiner = _joiner(laifu)
    caplog.set_level(logging.INFO)
    for _ in range(2):
        with pytest.raises(join_module.JoinError) as refused:
            _join(joiner)
        assert (refused.value.code, refused.value.status) == ("unsupported", 501)
    assert len([record for record in caplog.records if "gateway.hosted_room_peer" in record.getMessage()]) == 1
    assert laifu.grants.reserved == [] and laifu.api.calls == []


def test_a_hermes_without_room_setup_refuses_joins(laifu, monkeypatch):
    def unsupported(cls, env_scope=None):
        raise setups.SetupError("unsupported", "This Hermes is missing what Linked Rooms setup needs", 501)

    monkeypatch.setattr(setups.Hermes, "load", classmethod(unsupported))
    joiner = join_module.Joiner(laifu.root, hermes=join_module.Hermes(laifu.root), clock=lambda: NOW)
    with pytest.raises(join_module.JoinError) as refused:
        joiner.check("default")
    assert (refused.value.code, refused.value.status) == ("unsupported", 501)


def test_an_api_server_that_doesnt_answer_leaves_nothing_behind(laifu):
    joiner = _joiner(laifu, api_url=lambda profile: "http://127.0.0.1:9")
    with pytest.raises(join_module.JoinError) as refused:
        _join(joiner)
    assert (refused.value.code, refused.value.status) == ("api_server", 503)
    (reserved,) = laifu.grants.reserved
    assert [entry[1] for entry in laifu.grants.revoked] == [reserved[1]]
    assert _bound(laifu) == []
