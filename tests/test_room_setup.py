"""Setting a Hermes up for rooms with other people's agents: the API server,
each agent's room ceiling and approvals, and the gateway restart
(designs/cross-owner-agents, N3c, decision 13)."""
import argparse
import hashlib
import importlib.util
import json
import logging
import pathlib
import sys
import tempfile
import threading
import types

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"

if "hermes_constants" not in sys.modules:
    _hermes_constants = types.ModuleType("hermes_constants")
    _hermes_constants.get_hermes_home = lambda: pathlib.Path(tempfile.gettempdir())
    sys.modules["hermes_constants"] = _hermes_constants
if "conduit_push" not in sys.modules:
    _pkg = types.ModuleType("conduit_push")
    _pkg.__path__ = [str(ROOT)]
    sys.modules["conduit_push"] = _pkg


def _load(name, path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_load("conduit_push.room_identity", ROOT / "room_identity.py")
setup = _load("conduit_push.room_setup", ROOT / "room_setup.py")

_api_spec = importlib.util.spec_from_file_location("conduit_plugin_api_room_setup", ROOT / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(_api_spec)
_api_spec.loader.exec_module(api)

# Hermes' configurable toolsets as _get_effective_configurable_toolsets lists
# them (930e5ed), cut down, with two plugins' after the built-ins.
TOOLSETS = [
    ("web", "🔍 Web Search & Scraping", "web_search, web_extract"),
    ("terminal", "💻 Terminal & Processes", "terminal, process"),
    ("vision", "👁️  Vision / Image Analysis", "vision_analyze"),
    ("x_search", "🐦 X (Twitter) Search", "x_search (requires xAI OAuth or XAI_API_KEY)"),
    ("stt", "🎙️ Speech-to-Text", "voice transcription (gateway voice messages + voice mode)"),
    ("session_search", "🔎 Session Search", "search past conversations"),
    ("discord", "💬 Discord (read/participate)", "fetch messages, search members, create thread"),
    ("conduit", "🔌 Conduit", "Conduit's tools on your phone"),
    ("a2a", "🔌 A2A", "agent-to-agent client tools"),
]
BUILTIN = {"web", "terminal", "vision", "x_search", "stt", "session_search", "discord"}
KEY = "k" * 64
GATEWAY = (4242, 1792000000.5)  # the gateway's pid and start time


def _yaml(path):
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}


def _env(path):
    values = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            name, sep, value = line.partition("=")
            if sep and not line.lstrip().startswith("#"):
                values[name.strip()] = value.strip().strip("'\"")
    return values


def _label(label):
    # Hermes' gui_toolset_label: the leading emoji off.
    parts = label.strip().split(None, 1)
    if len(parts) == 2 and not any(ch.isascii() and ch.isalnum() for ch in parts[0]):
        return parts[1].strip()
    return label.strip()


class FakeHermes:
    """Hermes' calls as the switch makes them, over real files in a root
    home and its profiles."""

    def __init__(self, root):
        self.root = root
        self.plugins = {"conduit", "a2a"}
        self.toolsets = list(TOOLSETS)
        self.tools = {"web": ["web_extract", "web_search"], "terminal": ["process", "terminal"]}  # resolve_toolset
        self.pinned = set()        # config keys an administrator pinned
        self.env_refused = set()   # .env keys the write lock refuses
        self.managed_install = None
        self.not_served = {}       # profile home -> "standalone" or "parked"
        self.policy_fails = False
        self.status = None         # the root's gateway_state.json
        self.config_writes = []
        self.env_writes = []

    def home(self, profile):
        return self.root if profile == "default" else self.root / "profiles" / profile

    def serve(self, port=8642, state="connected", live=True, error=None):
        """The root's gateway, as its gateway_state.json records it."""
        platform = {"state": state, "listener_base": f"http://127.0.0.1:{port}",
                    "writer_pid": GATEWAY[0], "writer_start_time": GATEWAY[1]}
        if error:
            platform["error_message"] = error
        self.status = {"live": live, "gateway_state": "running" if live else "stopped",
                       "pid": GATEWAY[0], "start_time": GATEWAY[1], "platforms": {"api_server": platform}}

    def save_env(self, home, key, value):
        self.env_writes.append((home, key))
        path = home / ".env"
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        lines = [line for line in lines if not line.startswith(f"{key}=")] + [f"{key}={value}"]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def write_config(self, path, data):
        self.config_writes.append(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    def policy(self, profile, home):
        # execution_policy_mapping, from the profile's own config.yaml.
        if self.policy_fails:
            raise RuntimeError("gateway config is invalid")
        config = _yaml(home / "config.yaml")
        saved = (config.get("platform_toolsets") or {}).get("api_server")
        named = [name for name in saved if name != "no_mcp"] if isinstance(saved, list) else ["hermes-api-server"]
        mode = (config.get("approvals") or {}).get("mode", "manual")
        mode = "off" if mode is False else str(mode).lower()
        unsigned = {"version": 1, "target_profile": profile, "enabled_toolsets": sorted({*named, "bot_room"}),
                    "approval_mode": mode, "max_iterations": 90}
        digest = hashlib.sha256(json.dumps(unsigned, sort_keys=True).encode()).hexdigest()
        return {**unsigned, "policy_digest": digest}

    def adapter(self):
        return setup.Hermes(
            read_config=_yaml, write_config=self.write_config, read_env=_env, save_env=self.save_env,
            env_refusal=lambda key: f"Cannot set {key}: it is managed by your administrator."
            if key in self.env_refused else None,
            managed=lambda: self.managed_install,
            key_managed=lambda key: key in self.pinned,
            usable_key=lambda value: isinstance(value, str) and len(value.strip()) >= 16
            and value.strip().lower() not in {"changeme-changeme-changeme"},
            toolsets=lambda: list(self.toolsets), plugin_toolsets=lambda: set(self.plugins),
            toolset_tools=lambda name: list(self.tools.get(name, [])),
            builtin_toolsets=lambda: set(BUILTIN), allowed=lambda name: name not in {"discord"},
            label=_label, policy=self.policy, served=lambda home: self.not_served.get(home),
            runtime_status=lambda root: self.status if root == self.root else None,
            gateway_live=lambda record: bool(record.get("live")))


class Ports:
    """What answers on this computer's ports."""

    def __init__(self):
        self.taken = set()

    def answers(self, host, port):
        return port in self.taken


@pytest.fixture
def hermes(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    (root / "profiles" / "laifu").mkdir(parents=True)
    fake = FakeHermes(root)
    fake.ports = Ports()
    monkeypatch.setattr(setup, "_answers", fake.ports.answers)
    monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)
    return fake


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _state(hermes, profile="default", **kwargs):
    return setup.state(hermes.adapter(), hermes.home(profile), profile, **kwargs)


def _apply(hermes, profile="default", **kwargs):
    return setup.apply(hermes.adapter(), hermes.home(profile), profile, **kwargs)


def _ceiling(hermes, profile="default"):
    return (_yaml(hermes.home(profile) / "config.yaml").get("platform_toolsets") or {}).get("api_server")


# --- The API server ---------------------------------------------------------------


def test_setup_turns_the_api_server_on_with_a_new_key(hermes):
    before = _state(hermes)
    assert before["needs"] == ["api_server", "ceiling", "plugin_tools", "restart"]
    assert before["api_server"] == {"enabled": False, "key_set": False, "profile_key_set": False,
                                    "address": "127.0.0.1:8642", "running": False, "error": None}
    assert not before["ready"] and before["blockers"] == []

    after = _apply(hermes)

    key = _env(hermes.root / ".env")["API_SERVER_KEY"]
    assert len(key) == 64 and int(key, 16) >= 0
    assert "api_server_key" in after["changed"]
    assert after["api_server"]["enabled"] and after["api_server"]["key_set"]
    # Not serving it yet: the gateway restarts to start it.
    assert after["needs"] == ["restart"]
    assert key not in json.dumps(after) and key not in json.dumps(before)


@pytest.mark.parametrize("where", ["env", "config", "config_extra"])
def test_a_usable_key_is_kept(hermes, where):
    if where == "env":
        (hermes.root / ".env").write_text(f"API_SERVER_KEY={KEY}\n", encoding="utf-8")
    else:
        section = {"enabled": True, "key": KEY} if where == "config" else {"enabled": True, "extra": {"key": KEY}}
        _write(hermes.root / "config.yaml", {"platforms": {"api_server": section}})

    state = _state(hermes)
    assert state["api_server"]["enabled"] and state["api_server"]["key_set"]
    assert "api_server" not in state["needs"] and "restart" in state["needs"]
    hermes.serve()
    state = _state(hermes)
    assert state["api_server"] == {"enabled": True, "key_set": True, "profile_key_set": True,
                                   "address": "127.0.0.1:8642", "running": True, "error": None}

    after = _apply(hermes)
    assert "api_server_key" not in after["changed"]
    assert ("API_SERVER_KEY" in _env(hermes.root / ".env")) == (where == "env")
    assert after["needs"] == [] and after["ready"]


def test_a_key_in_config_yaml_alone_needs_the_api_server_turned_on(hermes):
    # Hermes starts the API server from config.yaml only when it says
    # enabled: true; a key from the environment turns it on by itself.
    _write(hermes.root / "config.yaml", {"platforms": {"api_server": {"key": KEY}}})
    state = _state(hermes)
    assert not state["api_server"]["enabled"] and state["api_server"]["key_set"]
    assert "api_server" in state["needs"]

    after = _apply(hermes)
    assert _yaml(hermes.root / "config.yaml")["platforms"]["api_server"] == {"key": KEY, "enabled": True}
    assert "api_server_enabled" in after["changed"] and "api_server_key" not in after["changed"]
    assert "API_SERVER_KEY" not in _env(hermes.root / ".env")


def test_a_placeholder_key_is_replaced(hermes):
    (hermes.root / ".env").write_text("API_SERVER_KEY=changeme-changeme-changeme\n", encoding="utf-8")
    assert "api_server" in _state(hermes)["needs"]
    _apply(hermes)
    assert _env(hermes.root / ".env")["API_SERVER_KEY"] != "changeme-changeme-changeme"


def test_an_explicit_disable_is_turned_on(hermes):
    (hermes.root / ".env").write_text(f"API_SERVER_KEY={KEY}\n", encoding="utf-8")
    _write(hermes.root / "config.yaml", {"platforms": {"api_server": {"enabled": False, "port": 8650}}})
    state = _state(hermes)
    assert not state["api_server"]["enabled"] and "api_server" in state["needs"]

    after = _apply(hermes)
    section = _yaml(hermes.root / "config.yaml")["platforms"]["api_server"]
    assert section == {"enabled": True, "port": 8650}
    assert "api_server_enabled" in after["changed"] and "api_server_key" not in after["changed"]
    assert after["api_server"]["address"] == "127.0.0.1:8650"


@pytest.mark.parametrize("config, path", [
    ({"gateway": {"platforms": {"api_server": {"enabled": "no"}}}}, ("gateway", "platforms", "api_server")),
    ({"gateway": {"api_server": {"enabled": False}}}, ("gateway", "api_server")),
    ({"gateway": {"platforms": {"api_server": {"enabled": False}}}, "platforms": {"api_server": {"enabled": None}}},
     ("platforms", "api_server")),
])
def test_a_disable_anywhere_hermes_reads_it_is_turned_on(hermes, config, path):
    (hermes.root / ".env").write_text(f"API_SERVER_KEY={KEY}\n", encoding="utf-8")
    _write(hermes.root / "config.yaml", config)
    assert not _state(hermes)["api_server"]["enabled"]
    _apply(hermes)
    section = _yaml(hermes.root / "config.yaml")
    for name in path:
        section = section[name]
    assert section["enabled"] is True
    assert _state(hermes)["api_server"]["enabled"]


def test_the_api_server_is_read_where_hermes_reads_it(hermes):
    # gateway.platforms, then platforms, then the gateway.api_server
    # shorthand: the later wins, as the gateway merges them, and a block's
    # extra wins over the blocks' own keys.
    (hermes.root / ".env").write_text(f"API_SERVER_KEY={KEY}\n", encoding="utf-8")
    config = {"gateway": {"platforms": {"api_server": {"port": 8651}}}}
    _write(hermes.root / "config.yaml", config)
    assert _state(hermes)["api_server"]["address"] == "127.0.0.1:8651"
    config["platforms"] = {"api_server": {"port": 8652}}
    _write(hermes.root / "config.yaml", config)
    assert _state(hermes)["api_server"]["address"] == "127.0.0.1:8652"
    config["gateway"]["api_server"] = {"port": 8653}
    _write(hermes.root / "config.yaml", config)
    assert _state(hermes)["api_server"]["address"] == "127.0.0.1:8653"
    config["platforms"]["api_server"]["extra"] = {"port": 8654}
    _write(hermes.root / "config.yaml", config)
    assert _state(hermes)["api_server"]["address"] == "127.0.0.1:8654"


def test_a_taken_port_moves_the_api_server(hermes):
    hermes.ports.taken |= {8642, 8643}  # someone else's servers

    assert _state(hermes)["api_server"]["address"] == "127.0.0.1:8644"   # where it will run
    after = _apply(hermes)

    assert _env(hermes.root / ".env")["API_SERVER_PORT"] == "8644"
    assert after["api_server"]["address"] == "127.0.0.1:8644"
    assert "api_server_port" in after["changed"]


def test_a_taken_port_moves_an_api_server_whose_key_is_in_config_yaml(hermes):
    # Wherever the key is, Hermes binds config.yaml's port, else the
    # environment's API_SERVER_PORT, which the gateway loads from the root
    # .env (api_server.listen_address).
    _write(hermes.root / "config.yaml", {"platforms": {"api_server": {"enabled": True, "key": KEY}}})
    hermes.ports.taken.add(8642)
    after = _apply(hermes)
    assert _env(hermes.root / ".env")["API_SERVER_PORT"] == "8643"
    assert after["api_server"]["address"] == "127.0.0.1:8643"


def test_a_port_already_set_is_kept_even_when_taken(hermes):
    (hermes.root / ".env").write_text("API_SERVER_PORT=8642\n", encoding="utf-8")
    hermes.ports.taken.add(8642)
    _apply(hermes)
    assert _env(hermes.root / ".env")["API_SERVER_PORT"] == "8642"


def test_the_port_of_an_api_server_that_ran_stays(hermes):
    hermes.serve(port=8642, state="stopped", live=False)
    hermes.ports.taken.add(8642)
    (hermes.root / ".env").write_text(f"API_SERVER_KEY={KEY}\n", encoding="utf-8")
    after = _apply(hermes)
    assert "API_SERVER_PORT" not in _env(hermes.root / ".env")
    assert after["needs"] == ["restart"]


def test_no_free_port_is_refused_before_anything_is_written(hermes, monkeypatch):
    monkeypatch.setattr(setup, "_answers", lambda host, port: True)
    with pytest.raises(setup.SetupError) as refused:
        _apply(hermes)
    assert refused.value.code == "no_port" and refused.value.status == 409
    assert hermes.config_writes == [] and hermes.env_writes == []


def test_an_api_server_open_to_the_lan_stays_open(hermes):
    (hermes.root / ".env").write_text(f"API_SERVER_KEY={KEY}\nAPI_SERVER_HOST=0.0.0.0\n", encoding="utf-8")
    hermes.serve()
    after = _apply(hermes)
    assert _env(hermes.root / ".env")["API_SERVER_HOST"] == "0.0.0.0"
    assert after["api_server"]["address"] == "0.0.0.0:8642"
    # This Hermes reaches it on its own address.
    assert setup.local_api_url(hermes.adapter(), hermes.root, "default") == "http://127.0.0.1:8642"


def test_the_env_wins_over_config_yaml_when_the_key_comes_from_it(hermes):
    (hermes.root / ".env").write_text(f"API_SERVER_KEY={KEY}\nAPI_SERVER_PORT=8700\n", encoding="utf-8")
    _write(hermes.root / "config.yaml", {"platforms": {"api_server": {"port": 8650}}})
    assert _state(hermes)["api_server"]["address"] == "127.0.0.1:8700"
    _write(hermes.root / "config.yaml", {"platforms": {"api_server": {"enabled": True, "port": 8650, "key": KEY}}})
    (hermes.root / ".env").write_text("API_SERVER_PORT=8700\n", encoding="utf-8")
    assert _state(hermes)["api_server"]["address"] == "127.0.0.1:8650"


def test_a_running_api_server_keeps_a_key_set_outside_hermes_files(hermes):
    # The key in the gateway's service file, which nothing here reads: the
    # gateway's own record says it serves the API server, so no new key.
    _write(hermes.root / "config.yaml", {"platforms": {"api_server": {"host": "0.0.0.0", "port": 9177}}})
    hermes.serve(port=9177)

    state = _state(hermes)
    assert state["api_server"] == {"enabled": True, "key_set": True, "profile_key_set": True,
                                   "address": "0.0.0.0:9177", "running": True, "error": None}
    assert "api_server" not in state["needs"] and "restart" not in state["needs"]
    _apply(hermes)
    assert "API_SERVER_KEY" not in _env(hermes.root / ".env")
    assert setup.local_api_url(hermes.adapter(), hermes.root, "default") == "http://127.0.0.1:9177"


def test_the_api_servers_error_reads_without_control_characters(hermes):
    (hermes.root / ".env").write_text(f"API_SERVER_KEY={KEY}\n", encoding="utf-8")
    hermes.serve(state="fatal", error="Port 8642 already in use\x1b[2J\nRetry later\x07")
    assert _state(hermes)["api_server"]["error"] == "Port 8642 already in use Retry later"


def test_a_gateway_record_with_a_broken_listener_reads_as_configured(hermes):
    (hermes.root / ".env").write_text(f"API_SERVER_KEY={KEY}\n", encoding="utf-8")
    hermes.serve(port="86a")
    state = _state(hermes)
    assert state["api_server"]["running"] and state["api_server"]["address"] == "127.0.0.1:8642"
    assert setup.local_api_url(hermes.adapter(), hermes.root, "default") == "http://127.0.0.1:8642"


@pytest.mark.parametrize("listener", ["http://0.0.0.0:8642", "http://[::]:8642"])
def test_a_wildcard_listener_is_reached_on_loopback(hermes, listener):
    (hermes.root / ".env").write_text(f"API_SERVER_KEY={KEY}\n", encoding="utf-8")
    hermes.serve()
    hermes.status["platforms"]["api_server"]["listener_base"] = listener
    assert setup.local_api_url(hermes.adapter(), hermes.root, "default") == "http://127.0.0.1:8642"


def test_a_stopped_gateway_that_served_the_api_server_isnt_given_a_new_key(hermes):
    hermes.serve(state="disconnected", live=False)
    after = _apply(hermes)
    assert "API_SERVER_KEY" not in _env(hermes.root / ".env")
    assert after["needs"] == ["restart"]


def _by_an_earlier_gateway(hermes):
    """The API server's entry as a gateway before the record's left it, which
    Hermes keeps across a restart."""
    hermes.status["platforms"]["api_server"].update(writer_pid=GATEWAY[0] - 1, writer_start_time=GATEWAY[1] - 60)


def test_an_earlier_gateways_entry_isnt_taken_as_running(hermes):
    (hermes.root / ".env").write_text(f"API_SERVER_KEY={KEY}\n", encoding="utf-8")
    hermes.serve(port=8650)
    _by_an_earlier_gateway(hermes)
    state = _state(hermes)
    assert not state["api_server"]["running"] and "restart" in state["needs"]
    assert setup.local_api_url(hermes.adapter(), hermes.root, "default") == "http://127.0.0.1:8642"
    hermes.serve(state="fatal", error="[Errno 98] address already in use")
    _by_an_earlier_gateway(hermes)
    assert _state(hermes)["api_server"]["error"] is None


@pytest.mark.parametrize("live", [True, False])
def test_an_earlier_gateways_entry_doesnt_stand_for_a_key(hermes, live):
    # The record's gateway didn't serve the API server, so its environment
    # has no key for it.
    hermes.serve(state="disconnected", live=live)
    _by_an_earlier_gateway(hermes)
    after = _apply(hermes)
    assert len(_env(hermes.root / ".env")["API_SERVER_KEY"]) == 64
    assert "api_server_key" in after["changed"]


def test_a_gateway_still_starting_keeps_an_earlier_entrys_key(hermes):
    hermes.serve(state="disconnected")
    _by_an_earlier_gateway(hermes)
    hermes.status["gateway_state"] = "starting"
    _apply(hermes)
    assert "API_SERVER_KEY" not in _env(hermes.root / ".env")


@pytest.mark.parametrize("live", [True, False])
def test_a_gateway_that_refused_a_missing_key_gets_one(hermes, live):
    # Hermes starts an API server config.yaml turns on without a key, and its
    # startup guard refuses it; a gateway with nothing else to run exits.
    _write(hermes.root / "config.yaml", {"platforms": {"api_server": {"enabled": True}}})
    hermes.serve(state="fatal", live=live, error="API_SERVER_KEY was rejected by the startup guard")
    hermes.status["platforms"]["api_server"]["error_code"] = "api_server_key_invalid"
    hermes.status["gateway_state"] = "running" if live else "startup_failed"
    after = _apply(hermes)
    assert len(_env(hermes.root / ".env")["API_SERVER_KEY"]) == 64
    assert "api_server_key" in after["changed"]


def test_restart_is_needed_only_while_the_api_server_isnt_running(hermes):
    (hermes.root / ".env").write_text(f"API_SERVER_KEY={KEY}\n", encoding="utf-8")
    assert "restart" in _state(hermes)["needs"]            # no gateway
    hermes.status = {"live": True, "gateway_state": "running", "platforms": {}}
    assert "restart" in _state(hermes)["needs"]            # a gateway without it
    hermes.serve(state="fatal", error="[Errno 98] address already in use")
    state = _state(hermes)
    assert "restart" in state["needs"]                     # it failed to start
    assert not state["api_server"]["running"] and "address already in use" in state["api_server"]["error"]
    hermes.serve()
    assert "restart" not in _state(hermes)["needs"]


# --- The ceiling --------------------------------------------------------------------


def test_the_ceiling_is_saved_with_mcp_and_plugins_kept_out(hermes):
    _write(hermes.root / "config.yaml", {
        "platform_toolsets": {"api_server": ["hermes-api-server"], "cli": ["hermes-cli"]},
        "known_plugin_toolsets": {"api_server": ["spotify"], "cli": ["a2a"]}})

    after = _apply(hermes, ceiling=["web", "terminal"])

    config = _yaml(hermes.root / "config.yaml")
    assert config["platform_toolsets"] == {"api_server": ["no_mcp", "terminal", "web"], "cli": ["hermes-cli"]}
    assert config["known_plugin_toolsets"] == {"api_server": ["a2a", "conduit", "spotify"], "cli": ["a2a"]}
    assert config["known_builtin_toolsets"]["api_server"] == sorted(BUILTIN)
    assert after["ceiling"] == ["terminal", "web"]
    assert after["policy"]["enabled_toolsets"] == ["bot_room", "terminal", "web"]
    assert {"ceiling", "plugin_tools"} <= set(after["changed"])


def test_conduits_own_tools_are_known_even_when_hermes_lists_no_plugins(hermes):
    hermes.plugins = set()
    _apply(hermes)
    assert _yaml(hermes.root / "config.yaml")["known_plugin_toolsets"]["api_server"] == ["conduit"]


def test_the_default_ceiling_is_what_room_choices_can_use(hermes):
    assert _state(hermes)["ceiling"] == ["session_search", "vision", "web", "x_search"]
    _apply(hermes)
    assert _ceiling(hermes) == ["no_mcp", "session_search", "vision", "web", "x_search"]


def test_a_default_hermes_doesnt_offer_is_left_out(hermes):
    hermes.toolsets = [row for row in TOOLSETS if row[0] != "x_search"]
    assert _state(hermes)["ceiling"] == ["session_search", "vision", "web"]


def test_a_saved_ceiling_stays_when_none_is_given(hermes):
    _apply(hermes, ceiling=["web"])
    assert _state(hermes)["ceiling"] == ["web"]
    after = _apply(hermes)
    assert _ceiling(hermes) == ["no_mcp", "web"]
    assert "ceiling" not in after["changed"]


def test_a_saved_ceiling_naming_a_toolset_hermes_no_longer_has_is_kept(hermes):
    _apply(hermes, ceiling=["web", "a2a"])
    hermes.plugins.discard("a2a")    # its plugin uninstalled
    hermes.toolsets = [row for row in hermes.toolsets if row[0] != "a2a"]
    approvals = _yaml(hermes.root / "config.yaml")
    approvals["approvals"] = {"mode": "off"}
    _write(hermes.root / "config.yaml", approvals)

    after = _apply(hermes)
    assert after["changed"] == ["approvals"]
    assert _ceiling(hermes) == ["a2a", "no_mcp", "web"]


def test_a_ceiling_saved_by_hand_is_replaced_by_the_default(hermes):
    _write(hermes.root / "config.yaml", {"platform_toolsets": {"api_server": ["web", "terminal"]}})
    state = _state(hermes)
    assert "ceiling" in state["needs"]
    assert state["ceiling"] == ["session_search", "vision", "web", "x_search"]


def test_an_empty_ceiling_is_talk_only(hermes):
    after = _apply(hermes, ceiling=[])
    assert _ceiling(hermes) == ["no_mcp"]
    assert after["ceiling"] == [] and after["policy"]["enabled_toolsets"] == ["bot_room"]


@pytest.mark.parametrize("ceiling", [["conduit"], ["web", "web2"], ["stt"], ["discord"], ["no_mcp"], "web",
                                     [3], ["web"] * 65, {"web": True}])
def test_conduit_or_an_unknown_toolset_is_refused(hermes, ceiling):
    with pytest.raises(setup.SetupError) as refused:
        _apply(hermes, ceiling=ceiling)
    assert refused.value.code == "bad_ceiling" and refused.value.status == 400
    assert hermes.config_writes == [] and hermes.env_writes == []
    with pytest.raises(setup.SetupError):
        _state(hermes, ceiling=ceiling)


def test_conduits_refusal_says_its_tools_stay_out(hermes):
    with pytest.raises(setup.SetupError) as refused:
        _apply(hermes, ceiling=["web", "conduit"])
    assert "Conduit's own tools" in str(refused.value)


def test_a_plugin_toolset_can_be_in_the_ceiling(hermes):
    after = _apply(hermes, ceiling=["a2a", "web"])
    assert _ceiling(hermes) == ["a2a", "no_mcp", "web"]
    assert after["ceiling"] == ["a2a", "web"]


def test_choices_are_the_toolsets_room_turns_can_have(hermes):
    choices = _state(hermes)["choices"]
    assert [choice["id"] for choice in choices] == ["web", "terminal", "vision", "x_search", "session_search", "a2a"]
    assert choices[0] == {"id": "web", "label": "Web Search & Scraping", "description": "web_search, web_extract"}


def test_choices_leave_out_toolsets_rooms_never_use(hermes):
    hermes.toolsets += [("memory", "🧠 Memory", "persistent memory"),
                        ("code_execution", "⚡ Code Execution", "execute_code"),
                        ("skills", "📚 Skills", "list, view, manage")]
    hermes.tools.update({"memory": ["memory"], "code_execution": ["execute_code"],
                         "skills": ["skill_manage", "skill_view", "skills_list"]})
    ids = [choice["id"] for choice in _state(hermes)["choices"]]
    assert "memory" not in ids and "code_execution" not in ids
    assert "skills" in ids and "a2a" in ids     # some tools rooms can use; tools Hermes can't list here
    with pytest.raises(setup.SetupError) as refused:
        _apply(hermes, ceiling=["memory"])
    assert refused.value.code == "bad_ceiling" and hermes.config_writes == []


# --- Approvals and what the switch can't fix ----------------------------------------------


@pytest.mark.parametrize("mode, saved", [(False, "manual"), ("off", "manual"), ("OFF", "manual"),
                                         ("smart", "smart"), ("manual", "manual")])
def test_approvals_off_becomes_manual_and_smart_stays(hermes, mode, saved):
    _write(hermes.root / "config.yaml", {"approvals": {"mode": mode, "timeout": 120}})
    before = _state(hermes)
    assert ("approvals" in before["needs"]) == (saved != str(mode).lower())
    after = _apply(hermes)
    assert _yaml(hermes.root / "config.yaml")["approvals"] == {"mode": saved, "timeout": 120}
    assert after["approvals"] == saved
    assert ("approvals" in after["changed"]) == (saved != str(mode).lower())


@pytest.mark.parametrize("where", ["process", "env_file"])
def test_yolo_mode_blocks_before_any_write(hermes, monkeypatch, where):
    if where == "process":
        monkeypatch.setenv("HERMES_YOLO_MODE", "1")
    else:
        (hermes.root / ".env").write_text("HERMES_YOLO_MODE=true\n", encoding="utf-8")
    state = _state(hermes)
    assert [blocker["code"] for blocker in state["blockers"]] == ["yolo"] and not state["ready"]
    with pytest.raises(setup.SetupError) as refused:
        _apply(hermes)
    assert refused.value.code == "yolo" and refused.value.status == 409
    assert hermes.config_writes == [] and hermes.env_writes == []


def test_a_managed_install_blocks_before_any_write(hermes):
    hermes.managed_install = "This Hermes installation is managed by Homebrew; it can't change its settings."
    with pytest.raises(setup.SetupError) as refused:
        _apply(hermes)
    assert refused.value.code == "managed" and "Homebrew" in str(refused.value)
    assert hermes.config_writes == [] and hermes.env_writes == []


@pytest.mark.parametrize("pinned", ["platform_toolsets.api_server", "known_plugin_toolsets.api_server",
                                    "approvals.mode"])
def test_a_pinned_setting_blocks_before_any_write(hermes, pinned):
    _write(hermes.root / "config.yaml", {"approvals": {"mode": "off"}})
    hermes.pinned.add(pinned)
    blockers = _state(hermes)["blockers"]
    assert blockers[0]["code"] == "managed" and pinned in blockers[0]["message"]
    with pytest.raises(setup.SetupError) as refused:
        _apply(hermes)
    assert refused.value.code == "managed" and pinned in str(refused.value)
    assert hermes.config_writes == [] and hermes.env_writes == []


def test_a_pinned_api_server_switch_blocks_turning_it_on(hermes):
    (hermes.root / ".env").write_text(f"API_SERVER_KEY={KEY}\n", encoding="utf-8")
    _write(hermes.root / "config.yaml", {"platforms": {"api_server": {"enabled": False}}})
    hermes.pinned.add("platforms.api_server.enabled")
    assert [blocker["code"] for blocker in _state(hermes)["blockers"]] == ["managed"]


def test_a_pinned_setting_the_switch_doesnt_change_doesnt_block(hermes):
    hermes.pinned.add("approvals.mode")
    assert _state(hermes)["blockers"] == []
    _apply(hermes)


def test_an_env_key_the_write_lock_refuses_blocks_before_any_write(hermes):
    hermes.env_refused.add("API_SERVER_KEY")
    with pytest.raises(setup.SetupError) as refused:
        _apply(hermes)
    assert refused.value.code == "managed" and "API_SERVER_KEY" in str(refused.value)
    assert hermes.config_writes == [] and hermes.env_writes == []


# --- Profiles --------------------------------------------------------------------------


def test_a_named_profile_gets_its_own_ceiling_and_the_roots_api_server(hermes):
    _write(hermes.root / "config.yaml", {"approvals": {"mode": "off"}, "platform_toolsets": {"cli": ["hermes-cli"]}})
    _write(hermes.home("laifu") / "config.yaml", {"approvals": {"mode": "off"}})

    after = _apply(hermes, "laifu", ceiling=["web"])

    laifu = _yaml(hermes.home("laifu") / "config.yaml")
    assert laifu["platform_toolsets"] == {"api_server": ["no_mcp", "web"]}
    assert laifu["approvals"] == {"mode": "manual"}
    # The root keeps its own ceiling and approvals; it gets the API server.
    assert _yaml(hermes.root / "config.yaml") == {"approvals": {"mode": "off"},
                                                   "platform_toolsets": {"cli": ["hermes-cli"]}}
    root_key = _env(hermes.root / ".env")["API_SERVER_KEY"]
    # Hermes serves a named profile at /p/<name> only with a key of its own.
    profile_key = _env(hermes.home("laifu") / ".env")["API_SERVER_KEY"]
    assert len(profile_key) == 64 and profile_key != root_key
    assert after["changed"][:2] == ["api_server_key", "profile_key"]
    assert after["api_server"]["profile_key_set"] is True
    assert after["profile"] == "laifu" and after["policy"]["target_profile"] == "laifu"
    assert setup.local_api_url(hermes.adapter(), hermes.home("laifu"), "laifu") == "http://127.0.0.1:8642/p/laifu"
    assert setup.local_api_url(hermes.adapter(), hermes.home("laifu"), "a b#c").endswith("/p/a%20b%23c")
    assert setup.local_api_url(hermes.adapter(), hermes.root, "default") == "http://127.0.0.1:8642"


def test_a_named_profile_without_its_own_key_gets_one_while_the_api_server_runs(hermes):
    (hermes.root / ".env").write_text(f"API_SERVER_KEY={KEY}\n", encoding="utf-8")
    hermes.serve()
    before = _state(hermes, "laifu")
    assert before["api_server"]["profile_key_set"] is False
    assert before["needs"][0] == "api_server" and before["changes"][0] == "profile_key"
    _apply(hermes, "laifu")
    assert _env(hermes.root / ".env")["API_SERVER_KEY"] == KEY
    assert hermes.env_writes == [(hermes.home("laifu"), "API_SERVER_KEY")]


def test_a_named_profiles_own_key_is_kept(hermes):
    (hermes.root / ".env").write_text(f"API_SERVER_KEY={KEY}\n", encoding="utf-8")
    hermes.serve()
    (hermes.home("laifu") / ".env").write_text(f"API_SERVER_KEY={'p' * 64}\n", encoding="utf-8")
    before = _state(hermes, "laifu")
    assert before["api_server"]["profile_key_set"] is True and "api_server" not in before["needs"]
    _apply(hermes, "laifu")
    assert _env(hermes.home("laifu") / ".env")["API_SERVER_KEY"] == "p" * 64 and hermes.env_writes == []


def test_the_default_profile_uses_the_api_servers_own_key(hermes):
    before = _state(hermes)
    assert before["api_server"]["profile_key_set"] is False and "profile_key" not in before["changes"]
    after = _apply(hermes)
    assert after["api_server"]["profile_key_set"] is True and "profile_key" not in after["changed"]
    assert hermes.env_writes == [(hermes.root, "API_SERVER_KEY")]


def test_the_state_says_what_a_setup_would_change(hermes):
    _write(hermes.root / "config.yaml", {"approvals": {"mode": "off"}})
    hermes.ports.taken.add(8642)
    assert _state(hermes)["changes"] == ["api_server_key", "api_server_port", "ceiling", "plugin_tools",
                                         "approvals"]
    assert _apply(hermes)["changes"] == []


def test_the_setup_writes_only_files_never_this_processs_environment(hermes, monkeypatch):
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    monkeypatch.setenv("API_SERVER_PORT", "9999")
    publish = hermes.save_env

    def save_and_publish(home, key, value):    # Hermes' save_env_value also sets os.environ
        publish(home, key, value)
        setup.os.environ[key] = value

    hermes.save_env = save_and_publish
    hermes.ports.taken.add(8642)
    _apply(hermes, "laifu")
    assert "API_SERVER_KEY" not in setup.os.environ and setup.os.environ["API_SERVER_PORT"] == "9999"


@pytest.mark.parametrize("why, code", [("standalone", "standalone_profile"), ("parked", "parked_profile")])
def test_a_profile_the_host_gateway_doesnt_serve_is_refused(hermes, why, code):
    hermes.not_served[hermes.home("laifu")] = why
    blockers = _state(hermes, "laifu")["blockers"]
    assert [blocker["code"] for blocker in blockers] == [code]
    assert "laifu" in blockers[0]["message"]
    with pytest.raises(setup.SetupError) as refused:
        _apply(hermes, "laifu")
    assert refused.value.code == code and refused.value.status == 409
    assert hermes.config_writes == [] and hermes.env_writes == []


# --- Writes ------------------------------------------------------------------------------


def test_every_other_setting_is_kept(hermes):
    original = {"model": {"default": "gpt-5", "provider": "openai"},
                "platform_toolsets": {"telegram": ["hermes-telegram"]},
                "approvals": {"mode": "smart", "timeout": 60},
                "platforms": {"api_server": {"cors_origins": ["https://example.com"]}, "telegram": {"enabled": True}},
                "agent": {"max_turns": 40, "disabled_toolsets": ["web"]}}
    _write(hermes.root / "config.yaml", original)

    _apply(hermes)

    config = _yaml(hermes.root / "config.yaml")
    for key in ("model", "approvals", "platforms", "agent"):
        assert config[key] == original[key]
    assert config["platform_toolsets"]["telegram"] == ["hermes-telegram"]


def test_the_default_profile_is_saved_in_one_write(hermes):
    _write(hermes.root / "config.yaml", {"approvals": {"mode": "off"}, "platforms": {"api_server": {"enabled": False}}})
    (hermes.root / ".env").write_text(f"API_SERVER_KEY={KEY}\n", encoding="utf-8")
    _apply(hermes)
    assert hermes.config_writes == [hermes.root / "config.yaml"]


def test_a_setup_that_changes_nothing_writes_nothing(hermes):
    (hermes.root / ".env").write_text(f"API_SERVER_KEY={KEY}\n", encoding="utf-8")
    hermes.serve()
    _apply(hermes)
    hermes.config_writes.clear()
    hermes.env_writes.clear()
    after = _apply(hermes)
    assert after["changed"] == [] and after["ready"]
    assert hermes.config_writes == [] and hermes.env_writes == []


def test_one_setup_at_a_time(hermes):
    started, release = threading.Event(), threading.Event()
    real = hermes.write_config

    def slow(path, data):
        started.set()
        release.wait(5)
        real(path, data)

    hermes.write_config = slow
    first = threading.Thread(target=lambda: _apply(hermes))
    first.start()
    assert started.wait(5)
    hermes.write_config = real

    other = threading.Thread(target=lambda: _apply(hermes, ceiling=["web"]))
    other.start()
    other.join(0.3)
    assert other.is_alive()  # waiting for the first's lock
    release.set()
    first.join(5)
    other.join(5)
    assert _ceiling(hermes) == ["no_mcp", "web"]


def test_the_policy_is_null_when_hermes_cant_read_it(hermes, caplog):
    hermes.policy_fails = True
    with caplog.at_level(logging.WARNING):
        assert _state(hermes)["policy"] is None
    assert "RuntimeError" in caplog.text


# --- Hermes without these calls ---------------------------------------------------------


def test_a_hermes_without_the_setup_calls_answers_501(monkeypatch, caplog):
    for name in ("hermes_cli", "hermes_cli.config"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    with caplog.at_level(logging.WARNING):
        with pytest.raises(setup.SetupError) as refused:
            setup.Hermes.load()
    assert refused.value.status == 501 and refused.value.code == "unsupported"
    assert "read_user_config_raw" in caplog.text


def test_the_key_never_leaves_the_host(hermes, capsys, caplog):
    with caplog.at_level(logging.DEBUG):
        after = _apply(hermes)
        _state(hermes)
    key = _env(hermes.root / ".env")["API_SERVER_KEY"]
    assert key not in json.dumps(after) and key not in caplog.text and key not in capsys.readouterr().out


# --- Routes ------------------------------------------------------------------------------


class Restarts:
    def __init__(self):
        self.asked = []
        self.refusal = None
        self.answer = {"restart_started": True, "restart_action": "gateway-restart", "restart_pid": 4242}

    def live_session_refusal(self, profile, verb):
        self.asked.append(("refusal", profile, verb))
        return self.refusal

    def after(self, profile, *, what, label):
        self.asked.append(("after", profile))
        return dict(self.answer)

    def spawn(self, profile=None, *, interrupt=False):
        self.asked.append(("spawn", profile, interrupt))
        return types.SimpleNamespace(pid=4243), False


@pytest.fixture
def client(hermes, monkeypatch):
    monkeypatch.setattr(api, "_calls_home", lambda: hermes.root)
    monkeypatch.setattr(api, "_room_setup", lambda: setup)
    monkeypatch.setattr(api, "_room_setup_hermes", lambda module: hermes.adapter())
    monkeypatch.setattr(api, "_room_setup_limiter", api._MintLimiter(100, 600.0))
    monkeypatch.setattr(api, "_room_setup_read_limiter", api._MintLimiter(100, 60.0))
    restarts = Restarts()
    gateway = types.ModuleType("hermes_cli.web_server_gateway")
    gateway.live_session_refusal = restarts.live_session_refusal
    gateway._restart_gateway_after = restarts.after
    web_server = types.ModuleType("hermes_cli.web_server")
    web_server._spawn_gateway_restart = restarts.spawn
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.__path__ = []
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server_gateway", gateway)
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server", web_server)
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    test_client = TestClient(app)
    test_client.restarts = restarts
    return test_client


def test_the_setup_route_reads_what_it_would_do(client, hermes):
    response = client.get(f"{BASE}/rooms/setup")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["ok"] and body["needs"] == ["api_server", "ceiling", "plugin_tools", "restart"]
    assert body["profile"] == "default" and body["ceiling"] == ["session_search", "vision", "web", "x_search"]
    assert hermes.config_writes == [] and client.restarts.asked == []


def test_the_setup_route_sets_up_and_restarts_through_hermes(client, hermes):
    response = client.post(f"{BASE}/rooms/setup", json={})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] and "api_server_key" in body["changed"]
    assert body["restart"] == {"started": True, "busy": False, "error": None}
    assert client.restarts.asked == [("refusal", "default", "restart"), ("after", "default")]
    assert _env(hermes.root / ".env")["API_SERVER_KEY"] not in response.text


def test_the_setup_route_doesnt_restart_when_the_api_server_runs(client, hermes):
    (hermes.root / ".env").write_text(f"API_SERVER_KEY={KEY}\n", encoding="utf-8")
    hermes.serve()
    body = client.post(f"{BASE}/rooms/setup", json={"ceiling": ["web"]}).json()
    assert body["changed"] == ["ceiling", "plugin_tools"] and "restart" not in body
    assert client.restarts.asked == []


def test_chats_running_come_back_as_busy(client, hermes):
    client.restarts.refusal = "2 sessions are running; use POST /api/gateway/restart?interrupt=true"
    body = client.post(f"{BASE}/rooms/setup", json={}).json()
    assert body["restart"] == {"started": False, "busy": True,
                               "error": "2 sessions are running; use POST /api/gateway/restart?interrupt=true"}
    assert ("after", "default") not in client.restarts.asked
    # The save stays.
    assert "API_SERVER_KEY" in _env(hermes.root / ".env")


def test_a_hermes_without_the_busy_check_restarts_through_its_own_path(client, hermes, monkeypatch):
    monkeypatch.delattr(sys.modules["hermes_cli.web_server_gateway"], "live_session_refusal")
    body = client.post(f"{BASE}/rooms/setup", json={}).json()
    assert body["restart"] == {"started": True, "busy": False, "error": None}
    assert client.restarts.asked == [("after", "default")]


def test_interrupt_forces_it(client, hermes):
    body = client.post(f"{BASE}/rooms/setup", json={"interrupt": True}).json()
    assert body["restart"] == {"started": True, "busy": False, "error": None}
    assert client.restarts.asked == [("spawn", "default", True)]


def test_interrupt_on_a_hermes_without_it_restarts_as_it_can(client, hermes, monkeypatch):
    def spawn(profile=None):
        client.restarts.asked.append(("spawn", profile))
        return types.SimpleNamespace(pid=1), False

    monkeypatch.setattr(sys.modules["hermes_cli.web_server"], "_spawn_gateway_restart", spawn)
    body = client.post(f"{BASE}/rooms/setup", json={"interrupt": True}).json()
    assert body["restart"]["started"]
    assert client.restarts.asked == [("spawn", "default")]


def test_a_hermes_that_cant_restart_from_here_says_so(client, hermes, monkeypatch):
    monkeypatch.delitem(sys.modules, "hermes_cli.web_server_gateway")
    monkeypatch.setattr(sys.modules["hermes_cli.web_server"], "_spawn_gateway_restart", None)
    body = client.post(f"{BASE}/rooms/setup", json={}).json()
    assert body["restart"]["started"] is False and "restart" in body["restart"]["error"]
    assert "API_SERVER_KEY" in _env(hermes.root / ".env")


def test_a_restart_that_fails_says_so_without_its_error(client, hermes, monkeypatch):
    def fails(profile, *, what, label):
        raise RuntimeError(f"can't start {hermes.root}/hermes-agent/venv/bin/python")

    monkeypatch.setattr(sys.modules["hermes_cli.web_server_gateway"], "_restart_gateway_after", fails)
    response = client.post(f"{BASE}/rooms/setup", json={})
    assert response.json()["restart"] == {"started": False, "busy": False, "error": api._ROOM_SETUP_RESTART_FAILED}
    assert "hermes-agent" not in response.text


def test_the_setup_route_takes_a_profile(client, hermes, monkeypatch):
    seen = []
    real = api._profile_scope

    def scope(profile):
        seen.append(profile)
        return real(None)

    monkeypatch.setattr(api, "_profile_scope", scope)
    monkeypatch.setattr(api, "_calls_home", lambda: hermes.home("laifu"))
    body = client.post(f"{BASE}/rooms/setup", params={"profile": "laifu"}, json={"ceiling": ["web"]}).json()
    assert body["profile"] == "laifu" and "laifu" in seen
    assert _ceiling(hermes, "laifu") == ["no_mcp", "web"]


def test_the_dashboard_writes_each_homes_env_in_its_own_profiles_scope(hermes, monkeypatch):
    # The root's key in the default profile's .env, a named profile's own key
    # in that profile's: never one in the other's.
    loaded = {}
    monkeypatch.setattr(setup.Hermes, "load", classmethod(lambda cls, env_scope=None: loaded.update(scope=env_scope)))
    seen = []
    monkeypatch.setattr(api, "_profile_scope", lambda profile: seen.append(profile))
    api._room_setup_hermes(setup)
    loaded["scope"](hermes.root)
    loaded["scope"](hermes.home("laifu"))
    assert seen == ["default", "laifu"]


@pytest.mark.parametrize("payload, code", [({"ceiling": ["conduit"]}, "bad_ceiling"), ({"ceiling": "web"}, "bad_ceiling"),
                                           ({"interrupt": "yes"}, "bad_request")])
def test_the_setup_route_refuses_with_a_code(client, hermes, payload, code):
    response = client.post(f"{BASE}/rooms/setup", json=payload)
    assert response.status_code == 400
    assert response.json()["code"] == code
    assert response.headers["cache-control"] == "no-store"
    assert hermes.config_writes == [] and hermes.env_writes == []


@pytest.mark.parametrize("raw", ['["web"]', "null", '"web"', "3"])
def test_the_setup_route_refuses_what_isnt_an_object(client, hermes, raw):
    response = client.post(f"{BASE}/rooms/setup", content=raw, headers={"Content-Type": "application/json"})
    assert response.status_code == 400 and response.json()["code"] == "bad_request"
    assert hermes.config_writes == [] and hermes.env_writes == []


def test_the_setup_route_refuses_yolo_with_409(client, hermes, monkeypatch):
    monkeypatch.setenv("HERMES_YOLO_MODE", "1")
    response = client.post(f"{BASE}/rooms/setup", json={})
    assert response.status_code == 409 and response.json()["code"] == "yolo"
    assert client.restarts.asked == []


def test_the_setup_route_on_a_hermes_without_the_calls_answers_501(client, monkeypatch):
    def unsupported(module):
        raise module.SetupError("unsupported", "This Hermes can't be set up for Linked Rooms here", 501)

    monkeypatch.setattr(api, "_room_setup_hermes", unsupported)
    response = client.get(f"{BASE}/rooms/setup")
    assert response.status_code == 501 and response.json()["code"] == "unsupported"


def test_setup_changes_are_limited(client, hermes, monkeypatch):
    monkeypatch.setattr(api, "_room_setup_limiter", api._MintLimiter(2, 600.0, message="Too many setup changes"))
    assert client.post(f"{BASE}/rooms/setup", json={"ceiling": ["web"]}).status_code == 200
    assert client.post(f"{BASE}/rooms/setup", json={"ceiling": ["web"]}).status_code == 200
    assert client.post(f"{BASE}/rooms/setup", json={"ceiling": ["web"]}).status_code == 429


def test_room_setup_is_a_listed_capability(client):
    assert "room-setup" in client.get(f"{BASE}/capabilities").json()["capabilities"]


# --- CLI --------------------------------------------------------------------------------


def _setup_args(ceiling=None, yes=False, no_restart=False):
    values = {"tools": None, "tool": None, "share": None, "search": None, "takes_part": None, "limit": None,
              "note": None, "room": ""}
    return argparse.Namespace(conduit_push_action="rooms", rooms_action="setup", yes=yes, ceiling=ceiling,
                              no_restart=no_restart, **values)


@pytest.fixture
def cli(hermes, monkeypatch):
    module = _load("conduit_push.cli", ROOT / "cli.py")
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", lambda: hermes.root)
    monkeypatch.setattr(setup.Hermes, "load", classmethod(lambda cls, **kwargs: hermes.adapter()))
    ran = []

    def call(command):
        ran.append(command)
        port = int(_env(hermes.root / ".env").get("API_SERVER_PORT", 8642))
        hermes.serve(port=port, **module.comes_up)    # the gateway's new record
        return 0

    monkeypatch.setattr(setup, "restart_command",
                        lambda: ["hermes", "-p", "default", "gateway", "restart", "--detach"])
    monkeypatch.setattr(module.subprocess, "call", call)
    monkeypatch.setattr(module, "RESTART_WAIT_S", 0.0)
    module.ran = ran
    module.comes_up = {}
    return module


def test_cli_setup_says_what_it_does_and_asks_first(cli, hermes, monkeypatch, capsys):
    answers = iter(["n", "y"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))

    assert cli.dispatch(_setup_args()) == 0
    said = capsys.readouterr().out
    assert "a new key" in said and "Web Search & Scraping" in said and "Cancelled." in said
    assert "127.0.0.1:8642" in said
    assert hermes.config_writes == [] and cli.ran == []

    assert cli.dispatch(_setup_args()) == 0
    said = capsys.readouterr().out
    assert "Saved." in said and "its API server runs at 127.0.0.1:8642" in said
    assert cli.ran == [["hermes", "-p", "default", "gateway", "restart", "--detach"]]
    assert _env(hermes.root / ".env")["API_SERVER_KEY"] not in said


def test_cli_setup_with_yes_and_no_restart(cli, hermes, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("asked"))
    assert cli.dispatch(_setup_args(ceiling="web,terminal", yes=True, no_restart=True)) == 0
    said = capsys.readouterr().out
    assert "hermes gateway restart" in said and cli.ran == []
    assert _ceiling(hermes) == ["no_mcp", "terminal", "web"]


def test_cli_setup_says_the_api_server_was_already_in_use(cli, hermes, monkeypatch, capsys):
    (hermes.root / ".env").write_text(f"API_SERVER_KEY={KEY}\n", encoding="utf-8")
    hermes.serve()
    assert cli.dispatch(_setup_args(yes=True)) == 0
    assert "other apps" in capsys.readouterr().out.lower()
    assert cli.ran == []


def test_cli_setup_when_its_already_set_up(cli, hermes, capsys):
    (hermes.root / ".env").write_text(f"API_SERVER_KEY={KEY}\n", encoding="utf-8")
    hermes.serve()
    assert cli.dispatch(_setup_args(yes=True)) == 0
    capsys.readouterr()
    assert cli.dispatch(_setup_args()) == 0
    assert "already set up" in capsys.readouterr().out


def test_cli_setup_says_a_changed_ceiling_stops_the_agent_in_rooms_already_started(cli, hermes, capsys):
    (hermes.root / ".env").write_text(f"API_SERVER_KEY={KEY}\n", encoding="utf-8")
    hermes.serve()
    assert cli.dispatch(_setup_args(ceiling="web,vision", yes=True)) == 0
    assert "already started" not in capsys.readouterr().out    # no room had a ceiling of the setup's yet

    assert cli.dispatch(_setup_args(ceiling="web", yes=True)) == 0
    said = capsys.readouterr().out
    assert ("then stops answering in rooms already started with other people's agents, which hold it to the "
            "tools it had when they started. New rooms use these.") in said
    assert said.index("already started") < said.index("Saved.")
    assert _ceiling(hermes) == ["no_mcp", "web"]

    hermes.serve(live=False)
    assert cli.dispatch(_setup_args(ceiling="web,vision", yes=True, no_restart=True)) == 0
    assert "already started" in capsys.readouterr().out    # the gateway down doesn't change what rooms hold


def test_cli_setup_names_what_blocks_it(cli, hermes, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_YOLO_MODE", "1")
    assert cli.dispatch(_setup_args(yes=True)) == 1
    assert "HERMES_YOLO_MODE" in capsys.readouterr().out
    assert hermes.config_writes == []


def test_cli_setup_refuses_a_bad_ceiling(cli, hermes, capsys):
    assert cli.dispatch(_setup_args(ceiling="web,conduit", yes=True)) == 1
    assert "Conduit's own tools" in capsys.readouterr().out
    assert hermes.config_writes == []


def test_cli_setup_without_a_terminal_cancels(cli, hermes, monkeypatch, capsys):
    def no_terminal(prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", no_terminal)
    assert cli.dispatch(_setup_args()) == 0
    assert "Cancelled." in capsys.readouterr().out
    assert hermes.config_writes == []


def test_cli_setup_says_when_the_restart_failed(cli, hermes, monkeypatch, capsys):
    monkeypatch.setattr(cli.subprocess, "call", lambda command: 78)
    assert cli.dispatch(_setup_args(yes=True)) == 1
    assert "hermes gateway restart" in capsys.readouterr().out


def test_cli_setup_says_when_the_api_server_didnt_come_up(cli, hermes, capsys):
    cli.comes_up = {"state": "fatal", "error": "No adapter available for enabled api_server."}
    assert cli.dispatch(_setup_args(yes=True)) == 1
    said = capsys.readouterr().out
    assert "isn't running: No adapter available for enabled api_server. See:" in said


def test_cli_setup_says_how_to_restart_on_a_hermes_that_cant_in_the_background(cli, hermes, monkeypatch, capsys):
    monkeypatch.setattr(setup, "restart_command", lambda: None)
    assert cli.dispatch(_setup_args(yes=True)) == 0
    assert "hermes gateway restart" in capsys.readouterr().out and cli.ran == []


def _hermes_cli_modules(monkeypatch, detach=True):
    launchers = types.ModuleType("hermes_cli._launchers")
    launchers.runtime_command = lambda root, args: ["/opt/hermes/python", "-m", "hermes_cli.main", *args]
    config = types.ModuleType("hermes_cli.config")
    config.get_project_root = lambda: "/opt/hermes"
    package = types.ModuleType("hermes_cli")
    package.__path__ = []
    modules = {"hermes_cli": package, "hermes_cli._launchers": launchers, "hermes_cli.config": config}
    if detach:
        start = types.ModuleType("hermes_cli.gateway_runtime_start")
        start.start_detached_replacement = lambda home, force=False: 0
        modules["hermes_cli.gateway_runtime_start"] = start
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    if not detach:
        monkeypatch.delitem(sys.modules, "hermes_cli.gateway_runtime_start", raising=False)


def test_the_restart_runs_in_the_background_where_hermes_can(monkeypatch):
    # Without a service manager, a plain `gateway restart` becomes the new
    # gateway in this terminal; `--detach` starts it in the background.
    _hermes_cli_modules(monkeypatch)
    assert setup.restart_command() == ["/opt/hermes/python", "-m", "hermes_cli.main", "-p", "default", "gateway",
                                       "restart", "--detach"]


def test_a_hermes_without_a_background_restart_isnt_restarted_from_the_cli(monkeypatch):
    _hermes_cli_modules(monkeypatch, detach=False)
    assert setup.restart_command() is None


def test_cli_setup_gives_a_named_profile_its_own_key(cli, hermes, monkeypatch, capsys):
    (hermes.root / ".env").write_text(f"API_SERVER_KEY={KEY}\n", encoding="utf-8")
    hermes.serve()
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", lambda: hermes.home("laifu"))
    assert cli.dispatch(_setup_args(yes=True)) == 0
    said = capsys.readouterr().out
    assert "(profile laifu)" in said and "a key of its own" in said and "API server: on, at 127.0.0.1:8642" in said
    assert _env(hermes.home("laifu") / ".env")["API_SERVER_KEY"] not in said and cli.ran == []


def test_cli_setup_says_why_the_api_server_isnt_running(cli, hermes, capsys):
    (hermes.root / ".env").write_text(f"API_SERVER_KEY={KEY}\n", encoding="utf-8")
    hermes.serve(state="fatal", error="Port 8642 already in use\x1b[2J")
    assert cli.dispatch(_setup_args(yes=True, no_restart=True)) == 0
    said = capsys.readouterr().out
    assert "Port 8642 already in use" in said and "\x1b" not in said
