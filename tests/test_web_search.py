import importlib.util
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]


def _load_plugin_api():
    spec = importlib.util.spec_from_file_location("conduit_plugin_api_search", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _load_plugin_api()


def hermes_reply(results=None, success=True, error=None):
    if not success:
        return json.dumps({"success": False, "error": error})
    return json.dumps({"success": True, "data": {"web": results or []}})


def test_search_returns_titles_urls_and_clipped_snippets():
    calls = []

    def search(query, limit):
        calls.append((query, limit))
        return hermes_reply([
            {"title": "Weather", "url": "https://example.com/w", "description": "Sunny  and\n 21°C", "position": 1},
            {"title": "No url", "description": "dropped"},
            {"title": "Long", "url": "https://example.com/l", "description": "x" * 900},
        ])

    result = api.run_web_search("  weather   in Toronto ", 5, search=search)

    assert calls == [("weather in Toronto", 5)]
    assert result["query"] == "weather in Toronto"
    assert result["results"][0] == {"title": "Weather", "url": "https://example.com/w", "snippet": "Sunny and 21°C"}
    assert [r["url"] for r in result["results"]] == ["https://example.com/w", "https://example.com/l"]
    assert len(result["results"][1]["snippet"]) == api.SEARCH_MAX_SNIPPET_CHARS


def test_search_clamps_the_limit():
    seen = []
    api.run_web_search("q", 50, search=lambda q, n: seen.append(n) or hermes_reply())
    api.run_web_search("q", "nope", search=lambda q, n: seen.append(n) or hermes_reply())
    assert seen == [api.SEARCH_MAX_RESULTS, 3]


@pytest.mark.parametrize("query", [None, "", "   ", "x" * (api.SEARCH_MAX_QUERY_CHARS + 1)])
def test_search_rejects_empty_or_huge_queries(query):
    with pytest.raises(api.TokenError) as err:
        api.run_web_search(query, search=lambda q, n: pytest.fail("must not search"))
    assert err.value.status == 400


def test_search_passes_hermes_error_through():
    with pytest.raises(api.TokenError) as err:
        api.run_web_search("q", search=lambda q, n: hermes_reply(success=False, error="No web search provider configured."))
    assert err.value.status == 502
    assert "No web search provider configured." in str(err.value)


def test_search_without_the_hermes_web_tool_is_unsupported():
    def missing(query, limit):
        raise ModuleNotFoundError("No module named 'tools'", name="tools")

    with pytest.raises(api.TokenError) as err:
        api.run_web_search("q", search=missing)
    assert err.value.status == 503


def test_search_is_rate_limited_per_profile(monkeypatch):
    monkeypatch.setattr(api, "_search_limiter", api._MintLimiter(1, 60.0, message="slow down"))
    api.run_web_search("q", search=lambda q, n: hermes_reply(), limiter_key="a")
    api.run_web_search("q", search=lambda q, n: hermes_reply(), limiter_key="b")
    with pytest.raises(api.TokenError) as err:
        api.run_web_search("q", search=lambda q, n: hermes_reply(), limiter_key="a")
    assert err.value.status == 429
    assert str(err.value) == "slow down"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(api, "_hermes_web_search", lambda q, n: hermes_reply([{"title": "T", "url": "https://e.com", "description": "d"}]))
    monkeypatch.setattr(api, "_hermes_web_search_status", lambda: {"available": True, "backend": "searxng"})
    monkeypatch.setattr(api, "_search_limiter", api._MintLimiter(api.SEARCH_LIMIT, api.SEARCH_WINDOW_S))
    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/conduit_push")
    return TestClient(app)


def test_routes_report_status_and_search(client):
    status = client.get("/api/plugins/conduit_push/web-search/status")
    assert status.json() == {"ok": True, "available": True, "backend": "searxng"}

    response = client.post("/api/plugins/conduit_push/web-search", json={"query": "news", "limit": 1})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"ok": True, "query": "news", "results": [{"title": "T", "url": "https://e.com", "snippet": "d"}]}


def test_route_reports_a_bad_query(client):
    response = client.post("/api/plugins/conduit_push/web-search", json={})
    assert response.status_code == 400
    assert response.json()["detail"] == "query is required"


def test_search_survives_an_overflowing_limit():
    seen = []
    api.run_web_search("q", float("inf"), search=lambda q, n: seen.append(n) or hermes_reply())
    assert seen == [3]


def test_search_keeps_only_web_urls():
    result = api.run_web_search("q", 5, search=lambda q, n: hermes_reply([
        {"title": "js", "url": "javascript:alert(1)"},
        {"title": "file", "url": "file:///etc/passwd"},
        {"title": "long", "url": "https://e.com/" + "a" * api.SEARCH_MAX_URL_CHARS},
        {"title": "ok", "url": "HTTPS://e.com/ok"},
    ]))
    assert [r["title"] for r in result["results"]] == ["ok"]


def test_search_errors_drop_urls_and_credentials():
    error = "GET https://search.internal:8080/search?q=x&api_key=abc123 failed: token=deadbeef; FIRECRAWL_API_KEY is not set"
    with pytest.raises(api.TokenError) as err:
        api.run_web_search("q", search=lambda q, n: hermes_reply(success=False, error=error))
    message = str(err.value)
    assert "search.internal" not in message and "abc123" not in message and "deadbeef" not in message
    assert "FIRECRAWL_API_KEY is not set" in message


def test_search_rejects_a_huge_query_before_normalizing():
    with pytest.raises(api.TokenError) as err:
        api.run_web_search(" " * (api.SEARCH_MAX_QUERY_CHARS * 4 + 1), search=lambda q, n: pytest.fail("must not search"))
    assert err.value.status == 400


def test_route_times_out_a_slow_backend(client, monkeypatch):
    import time

    monkeypatch.setattr(api, "SEARCH_TIMEOUT_S", 0.05)
    monkeypatch.setattr(api, "_hermes_web_search", lambda q, n: time.sleep(0.3) or hermes_reply())
    response = client.post("/api/plugins/conduit_push/web-search", json={"query": "slow"})
    assert response.status_code == 504
    assert response.headers["cache-control"] == "no-store"
