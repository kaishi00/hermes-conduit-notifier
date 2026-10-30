"""Chat history pages (hermes-conduit#1525): Hermes' own page, rows the phone
holds sent as their fingerprints."""

import importlib.util
import sys
import types
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"


def _load_plugin_api():
    spec = importlib.util.spec_from_file_location("conduit_plugin_api_history", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _load_plugin_api()

ROWS = [
    {"id": 1, "role": "user", "content": "hi", "timestamp": 1.0},
    {"id": 2, "role": "assistant", "content": "hello", "timestamp": 2.0},
    {"id": 3, "role": "user", "content": "more", "timestamp": 3.0},
]


@pytest.fixture
def hermes(monkeypatch):
    calls = []

    async def get_session_messages(session_id, profile=None, limit=None, offset=0, order=None,
                                   include_compacted=False, inline_images=True):
        calls.append({"session_id": session_id, "profile": profile, "limit": limit, "offset": offset,
                      "order": order, "include_compacted": include_compacted, "inline_images": inline_images})
        if session_id == "missing":
            raise HTTPException(status_code=404, detail="Session not found")
        return {"session_id": "stored-" + session_id, "profile": profile or "default", "messages": list(ROWS),
                "pagination": {"limit": limit, "offset": offset, "order": order, "returned": len(ROWS)}}

    sessions = types.ModuleType("hermes_cli.web_routers.sessions")
    sessions.get_session_messages = get_session_messages
    monkeypatch.setitem(sys.modules, "hermes_cli.web_routers.sessions", sessions)
    return calls


def _client():
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


def _post(body, query=""):
    return _client().post(f"{BASE}/sessions/history-page{query}", json=body)


def test_a_page_with_nothing_known_is_hermes_page_with_its_fingerprints(hermes):
    response = _post({"session_id": "s1", "limit": 120, "offset": 0, "order": "latest",
                      "include_compacted": True, "inline_images": False}, "?profile=work")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["messages"] == ROWS
    assert body["row_digests"] == [api.history.row_digest(row) for row in ROWS]
    assert body["session_id"] == "stored-s1"
    assert body["pagination"] == {"limit": 120, "offset": 0, "order": "latest", "returned": 3}
    # The same read Conduit makes of Hermes' route, in the profile it named.
    assert hermes == [{"session_id": "s1", "profile": "work", "limit": 120, "offset": 0, "order": "latest",
                       "include_compacted": True, "inline_images": False}]


def test_rows_the_phone_holds_come_as_their_fingerprints_alone(hermes):
    known = [api.history.row_digest(ROWS[0]), api.history.row_digest(ROWS[1])]
    body = _post({"session_id": "s1", "limit": 120, "order": "latest", "known": known}).json()
    assert body["messages"] == [{"conduit_known": known[0]}, {"conduit_known": known[1]}, ROWS[2]]
    assert body["row_digests"][:2] == known


def test_an_edited_row_comes_in_full():
    edited = {**ROWS[1], "content": "hello again"}
    known = [api.history.row_digest(row) for row in ROWS]
    page = api.history.history_page({"messages": [ROWS[0], edited, ROWS[2]]}, set(known))
    assert page["messages"] == [{"conduit_known": known[0]}, edited, {"conduit_known": known[2]}]


def test_a_fingerprint_ignores_key_order():
    assert api.history.row_digest({"a": 1, "b": [1, 2]}) == api.history.row_digest({"b": [1, 2], "a": 1})
    assert len(api.history.row_digest(ROWS[0])) == api.history.HISTORY_DIGEST_CHARS


def test_hermes_refusals_pass_through(hermes):
    response = _post({"session_id": "missing"})
    assert response.status_code == 404


def test_a_bad_request_is_refused(hermes):
    assert _post({"limit": 5}).status_code == 400
    assert _post({"session_id": "s1", "limit": -1}).status_code == 400
    assert _post({"session_id": "s1", "offset": "x"}).status_code == 400
    assert hermes == []


def test_without_hermes_route_the_route_says_so(monkeypatch):
    monkeypatch.setattr(api.history, "_session_routes", lambda: None)
    response = _post({"session_id": "s1"})
    assert response.status_code == 501
