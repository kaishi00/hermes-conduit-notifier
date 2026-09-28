import importlib.util
import sys
import threading
import time
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"


def _load_plugin_api():
    spec = importlib.util.spec_from_file_location("conduit_plugin_api_memory", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _load_plugin_api()


class FakeProvider:
    def __init__(self, name="honcho", available=True, block="## Honcho\nuser likes tea", recall="## Recall\nlikes tea"):
        self.name = name
        self.available = available
        self.block = block
        self.recall = recall
        self.initialized = []
        self.prefetched = []
        self.shut_down = False
        self.synced = False

    def is_available(self):
        return self.available

    def initialize(self, session_id, **kwargs):
        self.initialized.append((session_id, kwargs))

    def system_prompt_block(self):
        if isinstance(self.block, Exception):
            raise self.block
        return self.block

    def prefetch(self, query, *, session_id=""):
        self.prefetched.append((query, session_id))
        if isinstance(self.recall, Exception):
            raise self.recall
        return self.recall

    def sync_turn(self, *args, **kwargs):
        self.synced = True

    def shutdown(self):
        self.shut_down = True


@pytest.fixture
def hermes(monkeypatch):
    """Stub the Hermes side: config, built-in snapshot, provider loader."""
    state = types.SimpleNamespace(config={"memory": {}}, builtin="", providers={}, loads=[])

    def load(name):
        state.loads.append(name)
        return state.providers.get(name)

    agent_pkg = types.ModuleType("agent")
    memory_provider = types.ModuleType("agent.memory_provider")
    # Same sentinels as Hermes' agent/memory_provider.py.
    memory_provider.is_core_memory_provider = lambda name: str(name or "").strip().lower() in {
        "", "default", "builtin", "built-in", "none"}
    monkeypatch.setitem(sys.modules, "agent", agent_pkg)
    monkeypatch.setitem(sys.modules, "agent.memory_provider", memory_provider)
    monkeypatch.setattr(api, "_hermes_memory_config", lambda: state.config)
    monkeypatch.setattr(api, "_hermes_builtin_memory", lambda config: state.builtin)
    monkeypatch.setattr(api, "_hermes_load_memory_provider", load)
    monkeypatch.setattr(api, "_memory_provider_init_kwargs", lambda: {"platform": api.MEMORY_PLATFORM})
    monkeypatch.setattr(api, "_memory_providers", api._MemoryProviders())
    monkeypatch.setattr(api, "_memory_limiter", api._MintLimiter(api.MEMORY_RECALL_LIMIT, api.MEMORY_RECALL_WINDOW_S))
    return state


@pytest.fixture
def client(hermes):
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


def test_context_is_the_builtin_snapshot(hermes):
    hermes.builtin = "MEMORY (your personal notes)\nprefers metric"
    assert api.memory_context() == {
        "available": True, "provider": "builtin", "recall": False, "context": "MEMORY (your personal notes)\nprefers metric"}


def test_context_adds_the_external_provider_block(hermes):
    hermes.builtin = "notes"
    hermes.config = {"memory": {"provider": "honcho"}}
    provider = hermes.providers["honcho"] = FakeProvider()
    result = api.memory_context()
    assert result == {"available": True, "provider": "honcho", "recall": True, "context": "notes\n\n## Honcho\nuser likes tea"}
    assert provider.initialized == [(api.MEMORY_SESSION_ID, {"platform": api.MEMORY_PLATFORM})]
    assert not provider.synced


def test_context_with_nothing_stored_is_disabled(hermes):
    assert api.memory_context() == {"available": False, "provider": None, "recall": False, "context": "", "reason": "disabled"}


@pytest.mark.parametrize("name", ["", "builtin", "default", "none", " Built-in "])
def test_core_provider_names_mean_no_external_provider(hermes, name):
    hermes.config = {"memory": {"provider": name}}
    hermes.builtin = "notes"
    result = api.memory_context()
    assert result["provider"] == "builtin" and result["recall"] is False
    assert hermes.loads == []


def test_context_without_hermes_memory_modules_is_unsupported(hermes, monkeypatch):
    def missing(config):
        raise ModuleNotFoundError("No module named 'tools'", name="tools")

    monkeypatch.setattr(api, "_hermes_builtin_memory", missing)
    assert api.memory_context() == {"available": False, "reason": "unsupported", "provider": None, "recall": False, "context": ""}


def test_a_broken_import_inside_hermes_is_not_reported_as_missing(hermes, monkeypatch):
    def broken(config):
        raise ModuleNotFoundError("No module named 'ruamel'", name="ruamel")

    monkeypatch.setattr(api, "_hermes_builtin_memory", broken)
    with pytest.raises(ModuleNotFoundError):
        api.memory_context()


def test_context_is_clipped(hermes):
    hermes.builtin = "x" * 20000
    assert len(api.memory_context()["context"]) == api.MEMORY_CONTEXT_MAX_CHARS


def test_unavailable_or_failing_provider_falls_back_to_builtin(hermes):
    hermes.builtin = "notes"
    hermes.config = {"memory": {"provider": "mem0"}}
    hermes.providers["mem0"] = FakeProvider("mem0", available=False)
    assert api.memory_context() == {"available": True, "provider": "builtin", "recall": False, "context": "notes"}


def test_provider_init_failure_never_breaks_the_route(hermes):
    class Exploding(FakeProvider):
        def initialize(self, session_id, **kwargs):
            raise RuntimeError("connect https://honcho.internal?key=secret failed")

    hermes.config = {"memory": {"provider": "honcho"}}
    hermes.providers["honcho"] = Exploding()
    assert api.memory_context()["reason"] == "disabled"


def test_a_failing_system_prompt_block_keeps_recall(hermes):
    hermes.config = {"memory": {"provider": "honcho"}}
    hermes.providers["honcho"] = FakeProvider(block=RuntimeError("boom"))
    assert api.memory_context() == {"available": True, "provider": "honcho", "recall": True, "context": ""}


def test_provider_is_cached_per_profile_and_rebuilt_on_change(hermes):
    hermes.config = {"memory": {"provider": "honcho"}}
    honcho = hermes.providers["honcho"] = FakeProvider()
    hermes.providers["mem0"] = FakeProvider("mem0")
    api.memory_context("a")
    api.memory_context("a")
    api.memory_context("b")
    assert hermes.loads == ["honcho", "honcho"]
    hermes.config = {"memory": {"provider": "mem0"}}
    assert api.memory_context("a")["provider"] == "mem0"
    assert honcho.shut_down


def test_an_unavailable_provider_is_retried_later(hermes):
    now = [0.0]
    api._memory_providers.clock = lambda: now[0]
    hermes.config = {"memory": {"provider": "honcho"}}
    api.memory_context()
    hermes.providers["honcho"] = FakeProvider()
    assert api.memory_context()["recall"] is False
    now[0] += api.MEMORY_PROVIDER_RETRY_S
    assert api.memory_context()["recall"] is True


def test_concurrent_requests_start_one_provider(hermes):
    started = []

    def slow_start(name):
        started.append(name)
        time.sleep(0.05)
        return FakeProvider()

    cache = api._MemoryProviders()
    threads = [threading.Thread(target=cache.get, args=("", "honcho", slow_start)) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert started == ["honcho"]


def test_recall_uses_the_provider_prefetch(hermes):
    hermes.config = {"memory": {"provider": "honcho"}}
    provider = hermes.providers["honcho"] = FakeProvider(recall="r" * 9000)
    result = api.run_memory_recall("  what   tea ")
    assert provider.prefetched == [("what tea", api.MEMORY_SESSION_ID)]
    assert result["available"] is True
    assert len(result["results"]) == api.MEMORY_RECALL_MAX_CHARS
    assert not provider.synced


def test_recall_without_a_provider_is_not_an_error(hermes):
    assert api.run_memory_recall("tea") == {"available": False, "results": ""}


def test_a_hermes_without_external_providers_uses_builtin_only(hermes, monkeypatch):
    monkeypatch.setitem(sys.modules, "agent.memory_provider", None)  # import raises ModuleNotFoundError
    hermes.config = {"memory": {"provider": "honcho"}}
    hermes.providers["honcho"] = FakeProvider()
    hermes.builtin = "notes"
    assert api.memory_context()["provider"] == "builtin"
    assert hermes.loads == []


def test_recall_without_hermes_modules_is_not_an_error(hermes, monkeypatch):
    def missing():
        raise ModuleNotFoundError("No module named 'hermes_cli'", name="hermes_cli")

    monkeypatch.setattr(api, "_hermes_memory_config", missing)
    assert api.run_memory_recall("tea") == {"available": False, "results": ""}


@pytest.mark.parametrize("query", [None, "", "   ", "x" * (api.MEMORY_MAX_QUERY_CHARS + 1),
                                   " " * (api.MEMORY_MAX_QUERY_CHARS * 4 + 1)])
def test_recall_rejects_empty_or_huge_queries(hermes, query):
    hermes.config = {"memory": {"provider": "honcho"}}
    provider = hermes.providers["honcho"] = FakeProvider()
    with pytest.raises(api.TokenError) as err:
        api.run_memory_recall(query)
    assert err.value.status == 400
    assert provider.prefetched == []


def test_recall_backend_error_text_never_reaches_the_client(hermes):
    hermes.config = {"memory": {"provider": "honcho"}}
    hermes.providers["honcho"] = FakeProvider(recall=RuntimeError("GET https://h.internal/?api_key=abc123 failed"))
    with pytest.raises(api.TokenError) as err:
        api.run_memory_recall("tea")
    assert err.value.status == 502
    assert str(err.value) == "The memory provider failed; the Hermes log has the details"


def test_recall_is_rate_limited_per_profile(hermes, monkeypatch):
    monkeypatch.setattr(api, "_memory_limiter", api._MintLimiter(1, 60.0, message="slow down"))
    api.run_memory_recall("q", limiter_key="a")
    api.run_memory_recall("q", limiter_key="b")
    with pytest.raises(api.TokenError) as err:
        api.run_memory_recall("q", limiter_key="a")
    assert err.value.status == 429


def test_routes_report_context_and_recall(client, hermes):
    hermes.builtin = "notes"
    hermes.config = {"memory": {"provider": "honcho"}}
    hermes.providers["honcho"] = FakeProvider()

    context = client.get(f"{BASE}/memory/context")
    assert context.headers["cache-control"] == "no-store"
    assert context.json() == {"ok": True, "available": True, "provider": "honcho", "recall": True,
                              "context": "notes\n\n## Honcho\nuser likes tea"}

    recall = client.post(f"{BASE}/memory/recall", json={"query": "tea"})
    assert recall.status_code == 200
    assert recall.headers["cache-control"] == "no-store"
    assert recall.json() == {"ok": True, "available": True, "results": "## Recall\nlikes tea"}


def test_recall_route_without_a_provider(client):
    assert client.post(f"{BASE}/memory/recall", json={"query": "tea"}).json() == {"ok": True, "available": False, "results": ""}


def test_recall_route_reports_a_bad_query(client):
    response = client.post(f"{BASE}/memory/recall", json={})
    assert response.status_code == 400
    assert response.json()["detail"] == "query is required"


def test_recall_route_refuses_an_oversized_body(client):
    response = client.post(f"{BASE}/memory/recall", content=b'{"query":"' + b"x" * 20000 + b'"}',
                           headers={"Content-Type": "application/json"})
    assert response.status_code == 413


def test_recall_route_times_out_a_slow_provider(client, hermes, monkeypatch):
    monkeypatch.setattr(api, "MEMORY_TIMEOUT_S", 0.05)
    hermes.config = {"memory": {"provider": "honcho"}}
    slow = FakeProvider()
    slow.prefetch = lambda query, session_id="": time.sleep(0.3) or "late"
    hermes.providers["honcho"] = slow
    response = client.post(f"{BASE}/memory/recall", json={"query": "tea"})
    assert response.status_code == 504
    assert response.headers["cache-control"] == "no-store"


def test_context_route_hides_unexpected_errors(client, hermes, monkeypatch):
    def broken(config):
        raise OSError("/home/secret/.hermes/memories unreadable")

    monkeypatch.setattr(api, "_hermes_builtin_memory", broken)
    response = client.get(f"{BASE}/memory/context")
    assert response.status_code == 500
    assert "secret" not in response.text
    assert response.json()["detail"] == "Memory context failed on the host (OSError)"


def test_builtin_snapshot_against_stub_hermes(monkeypatch, tmp_path):
    """The real _hermes_builtin_memory, with Hermes' memory module injected."""
    memories = tmp_path / "memories"
    stores = []

    class Store:
        def __init__(self, memory_limit, user_limit, *, memory_enabled, user_profile_enabled):
            self.args = (memory_limit, user_limit, memory_enabled, user_profile_enabled)
            stores.append(self)

        def load_from_disk(self):
            self.loaded = True

        def format_for_system_prompt(self, kind):
            return {"memory": "MEMORY block", "user": "USER block"}[kind]

    module = types.ModuleType("tools.memory_tool")
    module.MemoryStore = Store
    module.get_builtin_memory_config = lambda config: config.get("memory", {})
    module.get_builtin_memory_store_flags = lambda config: (
        config["memory"].get("memory_enabled", True), config["memory"].get("user_profile_enabled", True))
    module.get_memory_dir = lambda: memories
    monkeypatch.setitem(sys.modules, "tools", types.ModuleType("tools"))
    monkeypatch.setitem(sys.modules, "tools.memory_tool", module)

    config = {"memory": {"user_profile_enabled": False, "memory_char_limit": "nope"}}
    assert api._hermes_builtin_memory(config) == ""  # nothing stored yet: no directory created
    assert stores == [] and not memories.exists()

    memories.mkdir()
    assert api._hermes_builtin_memory(config) == "MEMORY block"
    assert stores[0].args == (2200, 1375, True, False) and stores[0].loaded
    assert api._hermes_builtin_memory({"memory": {}}) == "MEMORY block\n\nUSER block"
