import importlib.util
import sys
import types
import json
import re
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"


def _load_plugin_api():
    spec = importlib.util.spec_from_file_location("conduit_plugin_api_capabilities", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _load_plugin_api()


def _client():
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


def test_capabilities_report_the_manifest_version_and_route_features():
    response = _client().get(f"{BASE}/capabilities")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    body.pop("name", None)  # this machine's name: test_pairing.test_capabilities_name_this_hermes
    manifest = re.search(r"^version:\s*(\S+)", (ROOT / "plugin.yaml").read_text(), re.M).group(1)
    assert body == {"ok": True, "version": manifest, "loaded_version": manifest, "restart_needed": False,
                    "boot_id": api.capabilities.BOOT_ID, "capabilities": list(api.capabilities.ROUTE_CAPABILITIES)}
    assert "session-takeover" in body["capabilities"]
    # The dashboard manifest must carry the same version the route reports.
    assert json.loads((ROOT / "dashboard" / "manifest.json").read_text())["version"] == manifest


def test_every_capability_names_a_served_route_family():
    # A capability is a promise Conduit acts on: each one must match a route.
    paths = {route.path for route in api.router.routes}
    prefixes = {
        "gemini-live": "/gemini-live/", "qwen-live": "/qwen-live/", "web-search": "/web-search", "memory": "/memory/",
        "personality": "/personality", "gpt-live": "/gpt-live/", "grok-live": "/grok-live/",
        "voice-sessions": "/voice/sessions", "voice-tags": "/voice/tags", "voice-summary": "/voice/summary",
        "voice-chat-calls": "/voice/chat-calls",
        "session-takeover": "/sessions/takeover", "e2e-notifications": "/e2e", "e2e-inline-rotation": "/e2e/rotate",
        "watch-tools": "/watch-tools/",
        # Jobs, their follow-ups and Gemini Live tokens ride the watch-tools grant route.
        "watch-jobs": "/watch-tools/",
        "watch-job-follow-ups": "/watch-tools/",
        "watch-live-token": "/watch-tools/",
        "watch-chats": "/watch-tools/",
        "watch-audio": "/watch-audio/",
        # Local Live on the Watch rides the audio bridge and its status route.
        "watch-local-live": "/watch-audio/",
        "watch-local-live-approvals": "/watch-audio/",
        # Read-back servers ride the Local Live socket and the Watch bridge.
        "local-live-read-back": "/local-live/socket",
        "desktop-views": "/sessions/desktop-views",
        "turn-surface": "/turn-surface",
        "turn-surface-discard": "/turn-surface/discard",
        "turn-surface-windows": "/turn-surface",
        "history-page": "/sessions/history-page",
        "session-usage": "/sessions/usage",
        "reply-speeds": "/sessions/reply-speeds",
        "chat-kanban-tasks": "/sessions/kanban-tasks",
        "chat-live-work": "/sessions/live-work",
        "chat-done": "/chats/done",
        "bot-plugin": "/profiles/plugin",
        "hermes-calls": "/calls",
        "hermes-call-presence": "/calls/presence",
        "hermes-call-outcomes": "/calls/outcomes",
        "hermes-call-answers": "/calls/waiting",
        "call-rings": "/calls/rings",
        "pc-control": "/pc/",
        "pc-asks": "/pc/ask",
        "conduit-settings": "/conduit-settings",
        "conduit-prefs": "/conduit-prefs",
        "pulse": "/pulse",
        "pulse_feed": "/pulse/feed",
        "pulse_model": "/pulse",
        "pulse_nudge": "/pulse",
        "pulse_done": "/pulse/done",
        "pulse_acted": "/pulse/acted",
        "pulse_acted_chat": "/pulse/acted",
        "pulse_dismiss": "/pulse/dismiss",
        "pulse_daily_brief": "/pulse",
        "pulse_found_actions": "/pulse/done",
        # The note to Pulse is a setting on the /pulse control route.
        "pulse_note": "/pulse",
        "pulse_rest": "/pulse/dismiss",
        "pulse_card_why": "/pulse/feed",
        "pulse_quiet": "/pulse",
        # What Pulse learned shows at /pulse/learned; the levels it caps at /pulse/levels.
        "pulse_learns": "/pulse/learned",
        # What it learned can be reset there, and what it left out is listed at /pulse/left_out.
        "pulse_explains": "/pulse/left_out",
        # The night is a setting on the /pulse control route; Wrong time a reason /pulse/rate takes.
        "pulse_hours": "/pulse",
        # The look ahead is a setting on the /pulse control route; what it left out is in /pulse/left_out.
        "pulse_dates": "/pulse/left_out",
        # Deadlines from the inbox: a setting on the /pulse control route, listed at /pulse/memory.
        "pulse_inbox": "/pulse/memory",
        # Looking things up first: a setting on the /pulse control route; what a card's turn looked at is in the feed.
        "pulse_look": "/pulse/feed",
        # Suggestions with an Add button: their cards in the feed, and the add, dismiss and undo routes.
        "pulse_proposals": "/pulse/proposals/",
        # Where you are: a setting on the /pulse control route; a place on what /pulse/memory lists.
        "pulse_where": "/pulse/memory",
        # Hide private reminders: a setting on the /pulse control route.
        "pulse_private": "/pulse",
        # Next steps: their level is in /pulse/learned, their cards in the feed.
        "pulse_next": "/pulse/learned",
        # Cards drawn from their picks: each card's look in the feed.
        "pulse_looks": "/pulse/feed",
        # Walking past: the reply to a location batch, and a setting on the /pulse control route.
        "pulse_spots": "/location/ingest",
        "pulse_profile_exclusions": "/pulse",
        "pulse_profile_exclusion_toggle": "/pulse",
        "present-v1": "/present/",
        "health-sync": "/health/",
        "shortcut-inbox": "/inbox/",
        "voice-tools": "/voice-tools",
        "voice-readers": "/voice-context",
        "location-sync": "/location/",
        # A fresh lookup is the location status route's `locate` answer.
        "location-locate": "/location/status",
        # Sharing with the host's other profiles rides the ingest route.
        "location-profiles": "/location/ingest",
        "phone-health-profiles": "/phone/ingest",
        "phone-sync": "/phone/",
        "phone-alarms": "/phone/",
        "phone-contacts": "/phone/",
        "phone-shortcuts": "/phone/",
        "phone-contact-edits": "/phone/",
        "phone-photos": "/phone/",
        # sharing.turn_status, read from the phone store each turn.
        "phone-turn-status": "/phone/",
        "pairing": "/pair",
        "pairing-devices": "/pair/remove",
        "self-restart": "/self/restart",
        "self-update": "/self/update",
        "gateway-work": "/self/gateway-work",
        "cron-notifications": "/cron/notifications",
        "cron-bot-chat-reports": "/cron/notifications",
        "reply-sources": "/notifications/reply-sources",
        "chat-on-screen": "/notifications/on-screen",
        "chat-mute": "/notifications/muted-chats",
        "quiet-turns": "/notifications/quiet-turns",
        "predict": "/predict",
        "predict-custom-instructions": "/predict",
        "side-question": "/side-question",
        "live-steps": "/live-steps",
        "room-files": "/room-files",
        "room-file-pictures": "/room-files/data-url",
        "room-file-reads": "/room-files/data-url",
        "room-files-linked": "/room-files/data-url",
        "room-images": "/room-images",
        "speech-server": "/speech-server/",
        "local-live": "/local-live/",
        # Agent mode: the call's tools for an engine with its own front model.
        "local-live-agent": "/local-live/v1/tools",
        "artifacts": "/artifacts",
        "artifact-pins": "/artifacts/pins",
        "workspace-files": "/workspace/",
        "workspace-resolve": "/workspace/resolve",
        "workspace-resolve-folders": "/workspace/resolve",
        "workspace-copy": "/workspace/",
        "workspace-git": "/workspace/git/", "terminals": "/terminals",
        "workspace-preview": "/workspace/preview/",
        "room-rules": "/rooms/",
        "room-forget": "/rooms/sessions",
        "room-links": "/rooms/links",
        "room-own-links": "/rooms/links/own",
        "room-own-bots": "/rooms/own-bots",
        "room-setup": "/rooms/setup",
        "room-invites": "/rooms/drafts",
        "room-invites-remove": "/rooms/invites/remove",
        # ?conduit_profile= on the voice and Watch routes, Local Live's first.
        "dashboard-profiles": "/local-live/",
        "room-leave": "/rooms/leave",
        "room-approvals": "/rooms/approvals",
        "room-copies": "/rooms/copies",
        "room-posts": "/rooms/copies/messages",
        "room-looks": "/rooms/looks",
        "room-looks-you": "/rooms/looks",
        "room-chats": "/rooms/chats",
        # ?conduit_profile= on the rooms routes that need the dashboard process, the setup's first.
        "room-dashboard-profiles": "/rooms/setup",
        # Use my data's free/busy, set in the choices.
        "room-free-busy": "/rooms/choices",
        "room-run-work": "/rooms/choices",
        "link-asks": "/links/rules",
        "bot-workstreams": "/workstreams",
        "waiting-list": "/waiting",
        "bot-work-settings": "/workstreams/settings",
        "work-adopt": "/workstreams/adopt",
        "work-call-ended": "/workstreams/call-ended",
        "board-checklists": "/boards/checklists",
        "room-boards": "/boards/rooms",
        "desktop-boards": "/boards/desktop",
        "diff-cards": "/boards/diffs",
        "card-taps": "/boards/taps",
        "card-tap-drop": "/boards/taps",
        "review-cards": "/boards/reviews",
        "config-unset": "/config/unset",
        "media-models": "/media-models/",
    }
    assert set(prefixes) == set(api.capabilities.ROUTE_CAPABILITIES)
    for capability, prefix in prefixes.items():
        assert any(path.startswith(prefix) for path in paths), capability


def test_an_unreadable_manifest_reports_no_version(monkeypatch):
    monkeypatch.setattr(api.capabilities.os.path, "dirname", lambda path: "/nonexistent")
    assert api.capabilities._plugin_version() is None


def test_a_newer_version_on_disk_reads_as_restart_needed(monkeypatch):
    monkeypatch.setattr(api.capabilities, "_plugin_version", lambda: "99.0.0")
    body = _client().get(f"{BASE}/capabilities").json()
    assert body["version"] == "99.0.0"
    assert body["loaded_version"] == api.capabilities.LOADED_VERSION != "99.0.0"
    assert body["restart_needed"] is True


def test_an_unmanaged_host_reports_no_managed_key(monkeypatch):
    monkeypatch.delenv("CONDUIT_PUSH_MANAGED", raising=False)
    body = _client().get(f"{BASE}/capabilities").json()
    assert "managed" not in body and "managed_by" not in body


def test_a_managed_host_says_so(monkeypatch):
    for value, managed_by in (("1", None), ("true", None), ("YES", None),
                              ("The Farm", "The Farm"), ("  Family farm\n ", "Family farm"),
                              ("x" * 200, "x" * 80)):
        monkeypatch.setenv("CONDUIT_PUSH_MANAGED", value)
        body = _client().get(f"{BASE}/capabilities").json()
        assert body["managed"] is True
        assert body.get("managed_by") == managed_by
    for value in ("", "0", "false", "no", "off"):
        monkeypatch.setenv("CONDUIT_PUSH_MANAGED", value)
        assert "managed" not in _client().get(f"{BASE}/capabilities").json()


def test_a_control_character_never_reaches_the_phone(monkeypatch):
    monkeypatch.setenv("CONDUIT_PUSH_MANAGED", "Farm\x07\x1b[31m")
    assert _client().get(f"{BASE}/capabilities").json()["managed_by"] == "Farm[31m"


def test_a_host_that_keeps_disbanded_rooms_chats_says_so(monkeypatch):
    # cleanup_disbanded_rooms: off (room_settings, notifier #443), so Disband's warning doesn't say they go.
    settings = api.core._plugin_package_module("room_settings")
    monkeypatch.setattr(settings, "cleanup_disbanded", lambda home=None: "off")
    assert _client().get(f"{BASE}/capabilities").json()["room_chats_kept"] is True
    monkeypatch.setattr(settings, "cleanup_disbanded", lambda home=None: "delete")
    assert "room_chats_kept" not in _client().get(f"{BASE}/capabilities").json()

    def broken(home=None):
        raise OSError("config.yaml unreadable")

    monkeypatch.setattr(settings, "cleanup_disbanded", broken)
    assert "room_chats_kept" not in _client().get(f"{BASE}/capabilities").json()


def test_what_the_plugin_cant_do_on_this_hermes_is_listed_until_it_can(monkeypatch):
    # Notifier issue 834: a state the host's log alone used to say.
    degraded = api.core._plugin_package_module("degraded")
    monkeypatch.setitem(sys.modules, degraded._HOLDER, types.ModuleType(degraded._HOLDER))
    assert "degraded" not in _client().get(f"{BASE}/capabilities").json()
    degraded.note(degraded.WORKSTREAM_RECOVERY)
    degraded.note(degraded.WORKSPACE_FILES)
    assert _client().get(f"{BASE}/capabilities").json()["degraded"] == ["workspace-files", "workstream-recovery"]
    degraded.clear(degraded.WORKSPACE_FILES)
    assert _client().get(f"{BASE}/capabilities").json()["degraded"] == ["workstream-recovery"]

    def broken():
        raise RuntimeError("unreadable")

    monkeypatch.setattr(degraded, "current", broken)
    assert "degraded" not in _client().get(f"{BASE}/capabilities").json()


def _fake_basic_sign_in(monkeypatch, *, basic: bool, config):
    # Hermes' provider registry and config, as the dashboard serving the plugin has them.
    registry = types.ModuleType("hermes_cli.dashboard_auth.registry")
    registry.get_provider = lambda name, scope=None: object() if basic and name == "basic" else None
    dashboard_auth = types.ModuleType("hermes_cli.dashboard_auth")
    dashboard_auth.registry = registry
    config_module = types.ModuleType("hermes_cli.config")
    config_module.load_config = config if callable(config) else (lambda: config)
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.dashboard_auth = dashboard_auth
    hermes_cli.config = config_module
    for name, module in (("hermes_cli", hermes_cli), ("hermes_cli.dashboard_auth", dashboard_auth),
                         ("hermes_cli.dashboard_auth.registry", registry), ("hermes_cli.config", config_module)):
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.delenv("HERMES_DASHBOARD_BASIC_AUTH_SECRET", raising=False)
    _start_dashboard(monkeypatch)


def _start_dashboard(monkeypatch):
    # The secret is read as Hermes imports the plugin, when the dashboard starts.
    monkeypatch.setattr(api.capabilities, "_SECRET_MISSING_AT_START", api.capabilities._secret_is_missing())


def test_password_sign_in_without_a_secret_says_restarts_sign_everyone_out(monkeypatch):
    # Hermes signs a password sign-in with a key made per dashboard process when
    # dashboard.basic_auth.secret is unset, so each restart ends every session.
    _fake_basic_sign_in(monkeypatch, basic=True, config={"dashboard": {"basic_auth": {"username": "me"}}})
    assert _client().get(f"{BASE}/capabilities").json()["sign_in_ends_on_restart"] is True


def test_unexpanded_shell_text_counts_as_no_secret(monkeypatch):
    # Hermes ignores a secret holding the command instead of its output.
    secret = {"secret": "$(openssl rand -base64 32)"}
    _fake_basic_sign_in(monkeypatch, basic=True, config={"dashboard": {"basic_auth": secret}})
    assert _client().get(f"{BASE}/capabilities").json()["sign_in_ends_on_restart"] is True


def test_a_secret_in_config_or_the_environment_keeps_sign_ins(monkeypatch):
    secret = {"secret": "c2VjcmV0LXNlY3JldC1zZWNyZXQtc2VjcmV0"}
    _fake_basic_sign_in(monkeypatch, basic=True, config={"dashboard": {"basic_auth": secret}})
    assert "sign_in_ends_on_restart" not in _client().get(f"{BASE}/capabilities").json()

    _fake_basic_sign_in(monkeypatch, basic=True, config={})
    monkeypatch.setenv("HERMES_DASHBOARD_BASIC_AUTH_SECRET", "c2VjcmV0LXNlY3JldC1zZWNyZXQtc2VjcmV0")
    _start_dashboard(monkeypatch)
    assert "sign_in_ends_on_restart" not in _client().get(f"{BASE}/capabilities").json()


def test_shell_text_in_the_environment_counts_as_no_secret(monkeypatch):
    _fake_basic_sign_in(monkeypatch, basic=True, config={})
    monkeypatch.setenv("HERMES_DASHBOARD_BASIC_AUTH_SECRET", "`openssl rand -base64 32`")
    _start_dashboard(monkeypatch)
    assert _client().get(f"{BASE}/capabilities").json()["sign_in_ends_on_restart"] is True


def test_a_secret_added_after_the_dashboard_started_doesnt_clear_the_flag(monkeypatch):
    # Hermes read the secret as it started, so this process's sign-ins still end.
    _fake_basic_sign_in(monkeypatch, basic=True, config={})
    monkeypatch.setenv("HERMES_DASHBOARD_BASIC_AUTH_SECRET", "c2VjcmV0LXNlY3JldC1zZWNyZXQtc2VjcmV0")
    assert _client().get(f"{BASE}/capabilities").json()["sign_in_ends_on_restart"] is True


def test_an_unreadable_config_reports_nothing_and_still_answers(monkeypatch):
    def broken():
        raise ValueError("bad yaml")
    _fake_basic_sign_in(monkeypatch, basic=True, config=broken)
    assert api.capabilities._secret_is_missing() is None
    reply = _client().get(f"{BASE}/capabilities")
    assert reply.status_code == 200
    assert "sign_in_ends_on_restart" not in reply.json()


def test_a_broken_provider_registry_reports_nothing(monkeypatch):
    _fake_basic_sign_in(monkeypatch, basic=True, config={})
    def broken(name, scope=None):
        raise RuntimeError("registry not ready")
    monkeypatch.setattr(sys.modules["hermes_cli.dashboard_auth.registry"], "get_provider", broken)
    assert "sign_in_ends_on_restart" not in _client().get(f"{BASE}/capabilities").json()


def test_no_password_sign_in_or_no_registry_reports_nothing(monkeypatch):
    _fake_basic_sign_in(monkeypatch, basic=False, config={})
    assert "sign_in_ends_on_restart" not in _client().get(f"{BASE}/capabilities").json()

    _fake_basic_sign_in(monkeypatch, basic=True, config={})
    # An older Hermes, with no provider registry.
    monkeypatch.delattr(sys.modules["hermes_cli.dashboard_auth"], "registry")
    monkeypatch.setitem(sys.modules, "hermes_cli.dashboard_auth.registry", None)
    assert "sign_in_ends_on_restart" not in _client().get(f"{BASE}/capabilities").json()


def test_a_registry_that_fails_to_import_reports_nothing(monkeypatch):
    # A broken or partial Hermes install raising something other than ImportError.
    _fake_basic_sign_in(monkeypatch, basic=True, config={})

    class Exploding(types.ModuleType):
        def __getattr__(self, name):
            raise RuntimeError("half-installed")

    monkeypatch.delattr(sys.modules["hermes_cli.dashboard_auth"], "registry")
    monkeypatch.setitem(sys.modules, "hermes_cli.dashboard_auth", Exploding("hermes_cli.dashboard_auth"))
    monkeypatch.delitem(sys.modules, "hermes_cli.dashboard_auth.registry")
    reply = _client().get(f"{BASE}/capabilities")
    assert reply.status_code == 200
    assert "sign_in_ends_on_restart" not in reply.json()
