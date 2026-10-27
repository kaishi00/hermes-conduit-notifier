"""Starting a room over Links with this Hermes' own calls (designs/cross-owner-agents, N5a): room_start.Hermes, which
finds the running room service a draft starts in (the legacy one this process runs, or the canonical one of the
draft's profile where the dashboard runs in the gateway) and makes RoomLink's catalog, client and route as
groups.peer.register does, and where_from, which says where a dashboard request's draft starts.

Hermes' modules are fakes of c73cbcc4's (9e01b44 and 930e5ed have the same names).
"""
import fcntl
import sys
import types
from pathlib import Path

import pytest

from test_room_bridge_member import ROOT, _load

start_module = _load("conduit_push.room_start", ROOT / "room_start.py")

# gateway.session_identity.authenticated_subject of the dashboard's session-token owner.
CLI_OWNER = "subject:session-token::legacy-token-owner"


class Service:
    """A hosted room service, as its runtime says whether it runs."""

    def __init__(self, db_path, running=True):
        self.db_path = db_path
        self.running = running
        self.runtime = types.SimpleNamespace(status=lambda: {"running": self.running, "stopping": False})


class Catalog:
    @classmethod
    def from_mapping(cls, value):
        catalog = cls()
        catalog.value = dict(value)
        return catalog


class Client:
    def __init__(self, *, base_url, api_key, receipt_db_path):
        self.base_url, self.api_key, self.receipt_db_path = base_url, api_key, receipt_db_path


class PeerError(RuntimeError):
    pass


class Route:
    def __init__(self, **fields):
        self.fields = fields


class Hermes:
    """The Hermes modules room_start.Hermes imports, in sys.modules: RoomLink's (gateway.hosted_room_peer,
    gateway.hosted_rooms, tui_gateway.hosted_room_peer_http and _transport), the legacy service's lookup
    (tui_gateway.methods_groups) and the canonical ones' (gateway.session_authorities, gateway.session_identity),
    with the dashboard app's gateway runner (hermes_cli.web_server)."""

    def __init__(self, monkeypatch, tmp_path):
        self.monkeypatch = monkeypatch
        self.legacy = Service(tmp_path / "shared-state.db")
        self.authorities = {}
        self.runner = None
        self.looked_up = []
        self.links = []
        peer = self.module("gateway.hosted_room_peer", GatewayRoomCatalog=Catalog, PROTOCOL_VERSION=2,
                           validate_room_link_url=self.validate)
        self.module("gateway.hosted_rooms", local_authority_gateway_id=lambda: "install:furina")
        self.module("tui_gateway.hosted_room_peer_http", PeerRunsHTTPClient=Client, PeerRunsHTTPError=PeerError)
        self.module("tui_gateway.hosted_room_peer_transport", PeerMemberRoute=Route)
        self.module("tui_gateway.methods_groups", get_hosted_room_service=lambda: self.legacy
                    if self.legacy is not None and self.legacy.running else None)
        self.module("gateway.session_authorities", authority_for_home=self.authority_for_home)
        self.module("gateway.session_identity", authenticated_subject=lambda identity: "subject:{}:{}:{}".format(
            identity.get("provider") or "", identity.get("issuer") or "", identity.get("user_id")))
        self.web = self.module("hermes_cli.web_server")
        self.web.app = types.SimpleNamespace(state=types.SimpleNamespace(gateway_runner=None))
        self.peer = peer

    def module(self, name, **attributes):
        module = types.ModuleType(name)
        for attribute, value in attributes.items():
            setattr(module, attribute, value)
        self.monkeypatch.setitem(sys.modules, name, module)
        return module

    def missing(self, name):
        self.monkeypatch.setitem(sys.modules, name, None)

    def validate(self, value):
        self.links.append(value)
        return value.rstrip("/"), "loopback"

    def authority_for_home(self, runner, home):
        assert runner is self.runner
        self.looked_up.append(home)
        return self.authorities.get(home)

    def gateway(self, *homes):
        """The dashboard runs in the gateway, serving these profiles' homes, each with its running service."""
        self.runner = object()
        self.web.app.state.gateway_runner = self.runner
        for home in homes:
            self.authorities[home] = types.SimpleNamespace(
                hosted_room_service=Service(home / "state.db"), db=types.SimpleNamespace(db_path=home / "state.db"))


@pytest.fixture
def hermes(monkeypatch, tmp_path):
    return Hermes(monkeypatch, tmp_path)


@pytest.fixture
def root(tmp_path):
    return tmp_path / "hermes"


def test_a_legacy_draft_starts_in_the_service_this_process_runs(hermes, root):
    found = start_module.Hermes(root).find({"kind": "legacy"})
    assert (found.kind, found.service, found.subject) == ("legacy", hermes.legacy, None)

    hermes.legacy.running = False
    with pytest.raises(start_module.StartError) as waiting:
        start_module.Hermes(root).find({"kind": "legacy"})
    assert waiting.value.code == "no_room_service"


def test_a_canonical_draft_starts_in_its_profiles_service_and_is_its_owners(hermes, root):
    work = root / "profiles" / "work"
    hermes.gateway(root, work)
    found = start_module.Hermes(root).find({"kind": "canonical", "home": str(work), "subject": "owner"})
    assert (found.kind, found.service, found.subject) == ("canonical",
                                                          hermes.authorities[work].hosted_room_service, "owner")
    assert hermes.looked_up == [work]


def test_a_canonical_draft_waits_for_the_gateway_elsewhere_and_for_its_service(hermes, root):
    where = {"kind": "canonical", "home": str(root), "subject": "owner"}
    with pytest.raises(start_module.StartError) as elsewhere:
        start_module.Hermes(root).find(where)
    assert elsewhere.value.code == "elsewhere"
    hermes.monkeypatch.delitem(sys.modules, "hermes_cli.web_server")
    with pytest.raises(start_module.StartError) as elsewhere:
        start_module.Hermes(root).find(where)
    assert elsewhere.value.code == "elsewhere"

    hermes.monkeypatch.setitem(sys.modules, "hermes_cli.web_server", hermes.web)
    hermes.gateway(root / "profiles" / "work")
    with pytest.raises(start_module.StartError) as waiting:
        start_module.Hermes(root).find(where)  # the default profile isn't served
    assert waiting.value.code == "no_room_service"
    hermes.gateway(root)
    hermes.authorities[root].hosted_room_service.running = False
    with pytest.raises(start_module.StartError) as waiting:
        start_module.Hermes(root).find(where)
    assert waiting.value.code == "no_room_service"


def test_a_draft_from_the_cli_starts_in_the_gateways_service_or_else_the_legacy_one(hermes, root):
    found = start_module.Hermes(root).find({"kind": "any"})
    assert (found.kind, found.service) == ("legacy", hermes.legacy)

    hermes.gateway(root)
    found = start_module.Hermes(root).find({"kind": "any"})
    assert (found.kind, found.service, found.subject) == (
        "canonical", hermes.authorities[root].hosted_room_service, CLI_OWNER)


def _held_elsewhere(root):
    """Whether the installation's rooms from the CLI are held, as another process sees it."""
    with open(root / "conduit-rooms" / ".gateway-rooms.lock", "a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False


def test_the_gateways_rooms_loop_holds_the_rooms_from_the_cli_while_its_dashboard_runs_there(hermes, root):
    starter = start_module.Starter(root)
    starter.claim()
    assert not (root / "conduit-rooms").exists()

    hermes.gateway(root)
    starter.claim()
    starter.claim()
    assert _held_elsewhere(root)
    assert starter.find({"kind": "any"}).kind == "canonical"

    hermes.web.app.state.gateway_runner = None  # the gateway's dashboard stopped
    starter.claim()
    assert not _held_elsewhere(root)


def test_a_cli_draft_waits_for_the_gateway_when_its_dashboard_runs_beside_this_one(hermes, root):
    (root / "conduit-rooms").mkdir(parents=True)
    with open(root / "conduit-rooms" / ".gateway-rooms.lock", "a+b") as gateway:
        fcntl.flock(gateway.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # the gateway's rooms loop, in its process
        with pytest.raises(start_module.StartError) as elsewhere:
            start_module.Starter(root).find({"kind": "any"})
        assert elsewhere.value.code == "elsewhere"
        # A draft this dashboard's own request made still starts in its service.
        assert start_module.Starter(root).find({"kind": "legacy"}).service is hermes.legacy
    found = start_module.Starter(root).find({"kind": "any"})
    assert (found.kind, found.service) == ("legacy", hermes.legacy)


@pytest.mark.parametrize("module", ["gateway.hosted_room_peer", "gateway.hosted_rooms",
                                    "tui_gateway.hosted_room_peer_http", "tui_gateway.hosted_room_peer_transport"])
def test_a_hermes_without_roomlink_is_unsupported(hermes, root, module):
    hermes.missing(module)
    calls = start_module.Hermes(root)
    assert calls.supported() == "unsupported"
    with pytest.raises(start_module.StartError) as unsupported:
        calls.find({"kind": "legacy"})
    assert (unsupported.value.code, unsupported.value.status) == ("unsupported", 501)
    assert module in str(unsupported.value)


def test_a_hermes_with_neither_room_service_lookup_is_unsupported(hermes, root):
    assert start_module.Hermes(root).supported() is None
    hermes.missing("tui_gateway.methods_groups")
    assert start_module.Hermes(root).supported() is None  # the canonical lookup is still there
    hermes.missing("gateway.session_authorities")
    calls = start_module.Hermes(root)
    assert calls.supported() == "unsupported"
    with pytest.raises(start_module.StartError) as unsupported:
        calls.find({"kind": "any"})
    assert unsupported.value.code == "unsupported"


def test_roomlinks_catalog_client_and_route_are_hermes_own(hermes, root):
    calls = start_module.Hermes(root)
    assert calls.installation() == "install:furina"
    assert calls.catalog({"installation_id": "install:laifu"}).value == {"installation_id": "install:laifu"}
    assert calls.link_url("http://127.0.0.1:4242/l/link1/p/default/") == "http://127.0.0.1:4242/l/link1/p/default"
    client = calls.client("http://127.0.0.1:4242/l/link1/p/default", Path("/db"))
    assert (client.base_url, client.api_key, client.receipt_db_path) == (
        "http://127.0.0.1:4242/l/link1/p/default", "", Path("/db"))
    assert calls.route(member_id="l1-1", grant="g").fields == {"member_id": "l1-1", "grant": "g"}
    assert calls.error is PeerError and calls.protocol_version == 2


def test_a_starter_without_hermes_calls_given_uses_this_hermes(hermes, root):
    starter = start_module.Starter(root)
    assert starter.installation() == "install:furina" and starter.supported() is None


class Request:
    def __init__(self, authority=None):
        self.app = types.SimpleNamespace(state=types.SimpleNamespace(session_authority=authority))


def test_a_dashboard_draft_starts_where_the_dashboard_runs_and_is_the_requests_subjects(hermes, root, monkeypatch):
    assert start_module.where_from(Request(), "work") == {"kind": "legacy"}

    work = root / "profiles" / "work"
    hermes.gateway(root, work)
    asked = []

    def context(request, profile):
        asked.append(profile)
        return hermes.authorities[work], types.SimpleNamespace(subject="owner", profile_id=str(work))

    hermes.module("hermes_cli.web_server_sessions", _session_mutation_context=context)
    where = start_module.where_from(Request(authority=hermes.authorities[root]), "work")
    assert where == {"kind": "canonical", "home": str(work), "subject": "owner"} and asked == ["work"]


def test_a_dashboard_in_a_gateway_without_its_request_context_cant_make_drafts(hermes, root):
    hermes.missing("hermes_cli.web_server_sessions")
    hermes.gateway(root)
    with pytest.raises(start_module.StartError) as unsupported:
        start_module.where_from(Request(authority=hermes.authorities[root]), None)
    assert (unsupported.value.code, unsupported.value.status) == ("unsupported", 501)
