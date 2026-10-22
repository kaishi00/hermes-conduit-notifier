import importlib.util
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
    manifest = re.search(r"^version:\s*(\S+)", (ROOT / "plugin.yaml").read_text(), re.M).group(1)
    assert body == {"ok": True, "version": manifest, "loaded_version": manifest, "restart_needed": False,
                    "boot_id": api.BOOT_ID, "capabilities": list(api.ROUTE_CAPABILITIES)}
    assert "session-takeover" in body["capabilities"]
    # The dashboard manifest must carry the same version the route reports.
    assert json.loads((ROOT / "dashboard" / "manifest.json").read_text())["version"] == manifest


def test_every_capability_names_a_served_route_family():
    # A capability is a promise Conduit acts on: each one must match a route.
    paths = {route.path for route in api.router.routes}
    prefixes = {
        "gemini-live": "/gemini-live/", "web-search": "/web-search", "memory": "/memory/",
        "personality": "/personality", "gpt-live": "/gpt-live/", "grok-live": "/grok-live/",
        "voice-sessions": "/voice/sessions", "voice-tags": "/voice/tags", "voice-summary": "/voice/summary",
        "voice-chat-calls": "/voice/chat-calls",
        "session-takeover": "/sessions/takeover", "e2e-notifications": "/e2e", "watch-tools": "/watch-tools/",
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
        "hermes-calls": "/calls",
        "hermes-call-presence": "/calls/presence",
        "hermes-call-outcomes": "/calls/outcomes",
        "conduit-settings": "/conduit-settings",
        "conduit-prefs": "/conduit-prefs",
        "pulse": "/pulse",
        "pulse_feed": "/pulse/feed",
        "present-v1": "/present/",
        "health-sync": "/health/",
        "voice-tools": "/voice-tools",
        "location-sync": "/location/",
        # A fresh lookup is the location status route's `locate` answer.
        "location-locate": "/location/status",
        "phone-sync": "/phone/",
        "phone-alarms": "/phone/",
        "phone-contacts": "/phone/",
        "pairing": "/pair",
        "self-restart": "/self/restart",
        "cron-notifications": "/cron/notifications",
        "reply-sources": "/notifications/reply-sources",
        "room-files": "/room-files",
        "room-images": "/room-images",
        "speech-server": "/speech-server/",
        "local-live": "/local-live/",
        # Agent mode: the call's tools for an engine with its own front model.
        "local-live-agent": "/local-live/v1/tools",
        "artifacts": "/artifacts",
    }
    assert set(prefixes) == set(api.ROUTE_CAPABILITIES)
    for capability, prefix in prefixes.items():
        assert any(path.startswith(prefix) for path in paths), capability


def test_an_unreadable_manifest_reports_no_version(monkeypatch):
    monkeypatch.setattr(api.os.path, "dirname", lambda path: "/nonexistent")
    assert api._plugin_version() is None


def test_a_newer_version_on_disk_reads_as_restart_needed(monkeypatch):
    monkeypatch.setattr(api, "_plugin_version", lambda: "99.0.0")
    body = _client().get(f"{BASE}/capabilities").json()
    assert body["version"] == "99.0.0"
    assert body["loaded_version"] == api.LOADED_VERSION != "99.0.0"
    assert body["restart_needed"] is True
