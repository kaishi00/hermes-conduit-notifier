"""An open chat's self-improvement updates follow the setting as it is now."""
import importlib.util
import sys
import threading
import types
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def _load(name="conduit_review_notices"):
    spec = importlib.util.spec_from_file_location(name, ROOT / "review_notices.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Agent:
    def __init__(self, mode):
        # What Hermes put on the agent when the chat opened.
        self.memory_notifications = mode


@pytest.fixture
def hermes(monkeypatch, tmp_path):
    """Hermes' background review, the dashboard's session registry and its
    config reader, as far as the plugin touches them."""
    seen = []
    background_review = types.ModuleType("agent.background_review")

    def _run_review_in_thread(agent, messages_snapshot, prompt, task_cfg=None, review_run=None, **kwargs):
        seen.append(agent.memory_notifications)

    background_review._run_review_in_thread = _run_review_in_thread
    agent_package = types.ModuleType("agent")
    agent_package.background_review = background_review
    monkeypatch.setitem(sys.modules, "agent", agent_package)
    monkeypatch.setitem(sys.modules, "agent.background_review", background_review)

    launch = tmp_path / "launch"
    launch.mkdir()
    server = types.ModuleType("tui_gateway.server")
    server._sessions, server._sessions_lock, server._hermes_home = {}, threading.RLock(), launch
    monkeypatch.setitem(sys.modules, "tui_gateway.server", server)

    config_effective = types.ModuleType("hermes_cli.config_effective")

    def load_user_config_effective(path):
        path = Path(path)
        return (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}

    config_effective.load_user_config_effective = load_user_config_effective
    monkeypatch.setitem(sys.modules, "hermes_cli.config_effective", config_effective)

    module = _load()
    assert module.install()
    yield types.SimpleNamespace(module=module, review=background_review, server=server, launch=launch, seen=seen)
    module.uninstall()


def _write(home, text):
    Path(home, "config.yaml").write_text(text)


def _run(hermes, agent):
    # Hermes resolves the function off the module when the review thread starts.
    hermes.review._run_review_in_thread(agent, [], "prompt", task_cfg={}, review_run=None)


def test_open_chat_reads_the_setting_as_it_is_now(hermes):
    agent = Agent("on")
    hermes.server._sessions["sid"] = {"agent": agent, "profile_home": None}
    _write(hermes.launch, "display:\n  memory_notifications: off\n")
    _run(hermes, agent)
    assert hermes.seen == ["off"]
    _write(hermes.launch, "display:\n  memory_notifications: verbose\n")
    _run(hermes, agent)
    assert hermes.seen == ["off", "verbose"]


def test_reads_the_chats_own_profile(hermes, tmp_path):
    profile = tmp_path / "work"
    profile.mkdir()
    _write(hermes.launch, "display:\n  memory_notifications: on\n")
    _write(profile, "display:\n  memory_notifications: 'off'\n")
    agent = Agent("on")
    hermes.server._sessions["sid"] = {"agent": agent, "profile_home": str(profile)}
    _run(hermes, agent)
    assert hermes.seen == ["off"]


@pytest.mark.parametrize("text, expected", [
    ("display:\n  memory_notifications: false\n", "off"),
    ("display:\n  memory_notifications: true\n", "on"),
    ("display:\n  memory_notifications: default\n", "default"),
    ("display: {}\n", "on"),
    ("display:\n", "on"),
    ("", "on"),
])
def test_reads_it_the_way_hermes_does(hermes, text, expected):
    agent = Agent("off")
    hermes.server._sessions["sid"] = {"agent": agent, "profile_home": None}
    _write(hermes.launch, text)
    _run(hermes, agent)
    assert hermes.seen == [expected]


def test_bot_chat_reads_it_too(hermes):
    # Bot Chat is a dashboard session that follows its profile's config.
    agent = Agent("on")
    hermes.server._sessions["bot"] = {"agent": agent, "profile_home": None, "follow_profile_config": True}
    _write(hermes.launch, "display:\n  memory_notifications: off\n")
    _run(hermes, agent)
    assert hermes.seen == ["off"]


def test_other_agents_keep_their_own(hermes):
    # A messaging platform's agent, which Hermes sets every turn.
    _write(hermes.launch, "display:\n  memory_notifications: off\n")
    agent = Agent("verbose")
    _run(hermes, agent)
    assert hermes.seen == ["verbose"]


def test_an_unreadable_config_keeps_the_review_going(hermes, monkeypatch, caplog):
    def broken(path):
        raise OSError("unreadable")

    monkeypatch.setattr(sys.modules["hermes_cli.config_effective"], "load_user_config_effective", broken)
    agent = Agent("on")
    hermes.server._sessions["sid"] = {"agent": agent, "profile_home": None}
    with caplog.at_level("DEBUG"):
        _run(hermes, agent)
        _run(hermes, agent)
    assert hermes.seen == ["on", "on"]
    # Warned once, not on every review.
    assert [r.levelname for r in caplog.records if "self-improvement" in r.getMessage()] == ["WARNING", "DEBUG"]


def test_no_dashboard_in_this_process(hermes, monkeypatch):
    monkeypatch.delitem(sys.modules, "tui_gateway.server")
    agent = Agent("on")
    _run(hermes, agent)
    assert hermes.seen == ["on"]


def test_installs_once_and_puts_hermes_own_back(hermes):
    wrapper = hermes.review._run_review_in_thread
    assert hermes.module.install()
    assert hermes.review._run_review_in_thread is wrapper
    # A second load of the plugin wraps Hermes' own, not the first load's wrapper.
    second = _load("conduit_review_notices_second")
    hermes.module.uninstall()
    own = hermes.review._run_review_in_thread
    assert not getattr(own, "__conduit_review_notices__", False)
    hermes.review._run_review_in_thread = wrapper
    assert second.install()
    assert second._wrapped[0] is own
    second.uninstall()
    assert hermes.review._run_review_in_thread is own


def test_a_hermes_without_reviews(monkeypatch):
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.background_review", types.ModuleType("agent.background_review"))
    module = _load("conduit_review_notices_none")
    assert not module.install()
    module.uninstall()
