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
    assert body == {"ok": True, "version": manifest, "capabilities": list(api.ROUTE_CAPABILITIES)}
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
        "session-takeover": "/sessions/takeover",
    }
    assert set(prefixes) == set(api.ROUTE_CAPABILITIES)
    for capability, prefix in prefixes.items():
        assert any(path.startswith(prefix) for path in paths), capability


def test_an_unreadable_manifest_reports_no_version(monkeypatch):
    monkeypatch.setattr(api.os.path, "dirname", lambda path: "/nonexistent")
    assert api._plugin_version() is None
