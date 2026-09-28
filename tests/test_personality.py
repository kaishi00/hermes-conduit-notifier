import contextlib
import contextvars
import importlib.util
import pathlib
import sys
import tempfile
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"

# client.py imports hermes_constants at import time; same stub as the clarify tests.
if "hermes_constants" not in sys.modules:
    _hermes_constants = types.ModuleType("hermes_constants")
    _hermes_constants.get_hermes_home = lambda: pathlib.Path(tempfile.gettempdir())
    sys.modules["hermes_constants"] = _hermes_constants


def _load_plugin_api():
    spec = importlib.util.spec_from_file_location("conduit_plugin_api_personality", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_plugin():
    # A synthetic package of its own, so this doesn't collide with the one the
    # clarify tests build (Hermes' plugin loader does the same).
    name = "conduit_push_personality"
    pkg_spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(pkg_spec)
    sys.modules[name] = module
    pkg_spec.loader.exec_module(module)
    return module


api = _load_plugin_api()
plugin = _load_plugin()

_active_profile = contextvars.ContextVar("active_profile", default=None)


@pytest.fixture
def hermes(monkeypatch, tmp_path):
    """Stub Hermes: per-profile homes, profile scope, and load_soul_md."""
    state = types.SimpleNamespace(souls={}, calls=[], entered=[])

    def home():
        return tmp_path / (_active_profile.get() or "default")

    @contextlib.contextmanager
    def scope(profile):
        state.entered.append(profile)
        token = _active_profile.set(profile)
        try:
            yield
        finally:
            _active_profile.reset(token)

    def load_soul_md(context_length=None, home_override=None):
        state.calls.append(home_override)
        return state.souls.get(Path(home_override).name)

    constants = types.ModuleType("hermes_constants")
    constants.get_hermes_home = home
    profiles = types.ModuleType("hermes_cli.web_server_profiles")
    profiles._config_profile_scope = scope
    prompt_builder = types.ModuleType("agent.prompt_builder")
    prompt_builder.load_soul_md = load_soul_md
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server_profiles", profiles)
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.prompt_builder", prompt_builder)
    monkeypatch.setattr(api, "_personality_limiter",
                        api._MintLimiter(api.PERSONALITY_LIMIT, api.PERSONALITY_WINDOW_S))
    state.home = tmp_path
    return state


@pytest.fixture
def client(hermes):
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


# --- Route ------------------------------------------------------------------


def test_route_returns_the_soul(client, hermes):
    hermes.souls["default"] = "You are Judge Judy. Be brisk."
    response = client.get(f"{BASE}/personality")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"ok": True, "available": True, "text": "You are Judge Judy. Be brisk."}
    assert hermes.calls == [hermes.home / "default"]


@pytest.mark.parametrize("soul", [None, "", "   \n  "])
def test_route_without_a_soul_is_unavailable(client, hermes, soul):
    hermes.souls["default"] = soul
    assert client.get(f"{BASE}/personality").json() == {"ok": True, "available": False, "text": ""}


def test_text_is_capped(client, hermes):
    hermes.souls["default"] = "x" * 20000
    text = client.get(f"{BASE}/personality").json()["text"]
    assert len(text) == api.PERSONALITY_MAX_CHARS
    assert text.endswith("…")


def test_older_hermes_without_load_soul_md_is_unavailable(client, hermes, monkeypatch):
    # The module exists but lacks the name: plain ImportError naming the module.
    monkeypatch.setitem(sys.modules, "agent.prompt_builder", types.ModuleType("agent.prompt_builder"))
    assert client.get(f"{BASE}/personality").json() == {"ok": True, "available": False, "text": ""}


def test_hermes_without_the_agent_package_is_unavailable(hermes, monkeypatch):
    monkeypatch.setitem(sys.modules, "agent.prompt_builder", None)  # import raises ModuleNotFoundError
    assert api.personality() == {"available": False, "text": ""}


def test_a_broken_import_inside_hermes_is_not_reported_as_missing(hermes, monkeypatch):
    def broken():
        raise ModuleNotFoundError("No module named 'yaml'", name="yaml")

    monkeypatch.setattr(api, "_hermes_soul_md", broken)
    with pytest.raises(ModuleNotFoundError):
        api.personality()


def test_route_reads_the_requested_profile(client, hermes):
    hermes.souls["default"] = "default soul"
    hermes.souls["coder"] = "coder soul"
    assert client.get(f"{BASE}/personality?profile=coder").json()["text"] == "coder soul"
    assert client.get(f"{BASE}/personality").json()["text"] == "default soul"
    # Always enters the scope, even without a profile.
    assert hermes.entered == ["coder", None]
    assert hermes.calls == [hermes.home / "coder", hermes.home / "default"]


def test_named_profile_fails_closed_without_profile_scoping(client, hermes, monkeypatch):
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server_profiles", types.ModuleType("hermes_cli.web_server_profiles"))
    response = client.get(f"{BASE}/personality?profile=coder")
    assert response.status_code == 503
    assert hermes.calls == []


def test_unknown_profile_passes_hermes_error_through(client, hermes, monkeypatch):
    from fastapi import HTTPException

    @contextlib.contextmanager
    def scope(profile):
        if profile:
            raise HTTPException(status_code=404, detail="Profile not found")
        yield

    monkeypatch.setattr(sys.modules["hermes_cli.web_server_profiles"], "_config_profile_scope", scope)
    response = client.get(f"{BASE}/personality?profile=ghost")
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"


def test_route_hides_unexpected_errors(client, hermes, monkeypatch):
    def broken():
        raise OSError("/home/secret/.hermes/SOUL.md unreadable")

    monkeypatch.setattr(api, "_hermes_soul_md", broken)
    response = client.get(f"{BASE}/personality")
    assert response.status_code == 500
    assert "secret" not in response.text
    assert response.json()["detail"] == "Personality request failed on the host (OSError)"


def test_route_is_rate_limited_per_profile(client, hermes, monkeypatch):
    monkeypatch.setattr(api, "_personality_limiter", api._MintLimiter(1, 60.0, message="slow down"))
    assert client.get(f"{BASE}/personality").status_code == 200
    assert client.get(f"{BASE}/personality?profile=coder").status_code == 200
    limited = client.get(f"{BASE}/personality?profile=current")
    assert limited.status_code == 429
    assert limited.json()["detail"] == "slow down"
    assert limited.headers["cache-control"] == "no-store"


# --- pre_llm_call hook --------------------------------------------------------

VOICE_NOTE = (
    "[Note: this message is a delegation from a live spoken conversation. The text is a voice "
    "transcript ...]"
)


def _history(content, *, extra=()):
    return [{"role": "system", "content": "sys"}, {"role": "user", "content": "earlier"},
            {"role": "assistant", "content": "ok"}, *extra, {"role": "user", "content": content}]


def test_register_adds_the_hook():
    hooks = {}
    ctx = types.SimpleNamespace(profile_name="default", register_hook=lambda name, fn: hooks.setdefault(name, fn),
                                register_cli_command=lambda **kwargs: None)
    plugin.register(ctx)
    assert hooks["pre_llm_call"] is plugin._pre_llm_call


def test_hook_fires_on_a_voice_live_turn_with_string_content():
    content = f"{VOICE_NOTE}\n[Recent spoken conversation, newest last:\nuser: hi]\n\nwhat's the verdict?"
    result = plugin._pre_llm_call(conversation_history=_history(content), user_message="what's the verdict?")
    assert result == {"context": plugin.PERSONA_VOICE_NOTE}
    assert "*sets down the gavel*" in plugin.PERSONA_VOICE_NOTE


def test_hook_fires_on_a_voice_live_turn_with_list_content():
    content = [{"type": "text", "text": VOICE_NOTE}, {"type": "image_url", "image_url": {"url": "data:"}},
               {"type": "text", "text": "what is this?"}]
    assert plugin._pre_llm_call(conversation_history=_history(content)) == {"context": plugin.PERSONA_VOICE_NOTE}


def test_hook_uses_hermes_own_note_when_available(monkeypatch):
    voice_live = types.ModuleType("tools.voice_live")
    voice_live.VOICE_LIVE_TURN_NOTE = "[Note: a newer spoken-delegation wording]"
    monkeypatch.setitem(sys.modules, "tools", types.ModuleType("tools"))
    monkeypatch.setitem(sys.modules, "tools.voice_live", voice_live)
    monkeypatch.setattr(plugin, "_voice_live_prefix", None)
    assert plugin._pre_llm_call(conversation_history=_history("[Note: a newer spoken-delegation wording]\n\nhi"))
    assert plugin._pre_llm_call(conversation_history=_history(f"{VOICE_NOTE}\n\nhi")) is None


@pytest.mark.parametrize("history", [
    None,
    [],
    "not a list",
    _history("plain typed message"),
    _history(f"quoting it: {VOICE_NOTE}"),
    _history([{"type": "image_url", "image_url": {"url": "data:"}}]),
    # Only the current (last) user turn counts.
    [{"role": "user", "content": f"{VOICE_NOTE}\n\nold"}, {"role": "assistant", "content": "ok"},
     {"role": "user", "content": "typed now"}],
    [{"role": "assistant", "content": VOICE_NOTE}],
])
def test_hook_is_a_no_op_otherwise(history):
    assert plugin._pre_llm_call(conversation_history=history) is None


def test_hook_never_raises():
    class Exploding(list):
        def __reversed__(self):
            raise RuntimeError("boom")

    assert plugin._pre_llm_call(conversation_history=Exploding([{}])) is None
    assert plugin._pre_llm_call() is None
    assert plugin._pre_llm_call(conversation_history=[{"role": "user", "content": [None, 3, {"type": "text"}]}]) is None


def test_manifest_declares_every_registered_hook():
    hooks = []
    ctx = types.SimpleNamespace(profile_name="default", register_hook=lambda name, fn: hooks.append(name),
                                register_cli_command=lambda **kwargs: None)
    plugin.register(ctx)
    manifest = (ROOT / "plugin.yaml").read_text()
    declared = {line.strip()[2:] for line in manifest.split("hooks:", 1)[1].splitlines() if line.strip().startswith("- ")}
    assert set(hooks) <= declared


def test_a_hermes_that_refuses_the_hook_keeps_the_other_hooks():
    hooks = []

    def register_hook(name, fn):
        if name == "pre_llm_call":
            raise ValueError("unknown hook")
        hooks.append(name)

    ctx = types.SimpleNamespace(profile_name="default", register_hook=register_hook,
                                register_cli_command=lambda **kwargs: None)
    plugin.register(ctx)
    assert "post_llm_call" in hooks and "subagent_stop" in hooks


def test_hook_finds_the_note_in_a_later_text_part():
    content = [{"type": "text", "text": "[Note: something else first]"}, {"type": "text", "text": f"{VOICE_NOTE}\n\nhi"}]
    assert plugin._pre_llm_call(conversation_history=_history(content)) == {"context": plugin.PERSONA_VOICE_NOTE}


def test_older_load_soul_md_without_home_override_reads_the_scoped_home(client, hermes, monkeypatch):
    hermes.souls["work"] = "Work persona."

    def load_soul_md(context_length=None):
        return hermes.souls.get(sys.modules["hermes_constants"].get_hermes_home().name)

    monkeypatch.setattr(sys.modules["agent.prompt_builder"], "load_soul_md", load_soul_md)
    response = client.get(f"{BASE}/personality", params={"profile": "work"})
    assert response.status_code == 200
    assert response.json()["text"] == "Work persona."


def test_a_hung_read_times_out(client, hermes, monkeypatch):
    import threading

    release = threading.Event()

    def hung():
        release.wait(5)
        return "late"

    monkeypatch.setattr(api, "_hermes_soul_md", hung)
    monkeypatch.setattr(api, "PERSONALITY_TIMEOUT_S", 0.2)
    try:
        response = client.get(f"{BASE}/personality")
    finally:
        release.set()
    assert response.status_code == 504
    assert response.headers["cache-control"] == "no-store"
