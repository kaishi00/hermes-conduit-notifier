"""Work the plugin hands to a thread or timer keeps the Hermes profile that
started it, in a process that serves several profiles.

Hermes resolves a profile's home through a context variable (falling back to
the profile the process was launched for), and a new thread doesn't inherit
context variables. The resolver below works the same way.
"""
import contextvars
import importlib.util
import json
import pathlib
import sys
import tempfile
import threading
import time
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

if "hermes_constants" not in sys.modules:
    _hermes_constants = types.ModuleType("hermes_constants")
    _hermes_constants.get_hermes_home = lambda: pathlib.Path(tempfile.gettempdir())
    sys.modules["hermes_constants"] = _hermes_constants
if "tools.clarify_tool" not in sys.modules:
    _tools = sys.modules.setdefault("tools", types.ModuleType("tools"))
    _tools.__path__ = getattr(_tools, "__path__", [])
    _clarify_tool = types.ModuleType("tools.clarify_tool")

    def _strip_recommended(text):
        # Same semantics as test_clarify_loop's stub, whichever module loads first.
        stripped = str(text).strip()
        suffix = "(Recommended)"
        if stripped.casefold().endswith(suffix.casefold()):
            return stripped[: -len(suffix)].strip()
        return stripped

    _clarify_tool.strip_recommended = _strip_recommended
    sys.modules["tools.clarify_tool"] = _clarify_tool
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


def _private_copy(name, path):
    # A private copy: other test files replace functions on the shared modules.
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plugin = _load("conduit_push.__init__", ROOT / "__init__.py")
calls = plugin.calls
live_activity = plugin.live_activity
clarify_loop = plugin.clarify_loop
client = _private_copy("conduit_push._client_profiles_under_test", ROOT / "client.py")

_home = contextvars.ContextVar("hermes_home")


def _pair(home, name):
    home.mkdir(parents=True, exist_ok=True)
    (home / "conduit-push.json").write_text(json.dumps({
        "relay_url": f"https://relay.example/{name}",
        "credential": f"cred-{name}",
        "installation_id": f"inst-{name}",
    }))


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    launch, coder = tmp_path / "default", tmp_path / "profiles" / "coder"
    _pair(launch, "default")
    _pair(coder, "coder")

    def get_hermes_home():
        # The override when a profile is in scope, else the launch profile.
        return _home.get(launch)

    for module in (client, calls, plugin):
        monkeypatch.setattr(module, "get_hermes_home", get_hermes_home)
    monkeypatch.setattr(live_activity, "client", client)
    monkeypatch.setattr(clarify_loop, "client", client)
    posted = []
    done = threading.Event()

    def request_json(url, *, method, payload=None, credential="", timeout=15.0):
        posted.append((method, credential))
        done.set()
        return {"tracked": True}

    monkeypatch.setattr(client, "request_json", request_json)
    return types.SimpleNamespace(launch=launch, coder=coder, posted=posted, done=done, home=get_hermes_home)


def _as_coder(profiles, work):
    """Runs ``work`` the way Hermes runs coder's hooks: with coder's home in
    a context variable."""
    context = contextvars.copy_context()
    context.run(lambda: (_home.set(profiles.coder), work())[1])


def test_the_delivery_worker_sends_with_the_profile_that_accepted_the_event(profiles):
    _as_coder(profiles, lambda: client.enqueue({"event_id": "response:1", "type": "response.ready", "body": "hi"}))
    client._events.join()

    assert profiles.posted == [("POST", "cred-coder")]


def test_the_live_activity_thread_posts_with_its_profile(profiles):
    reporter = live_activity.LiveActivityReporter()
    _as_coder(profiles, lambda: reporter.turn_started("s1"))

    # Another test's reporter thread can still be winding down and post with
    # the launch profile, so look for this reporter's post rather than the first.
    deadline = time.monotonic() + 5
    while ("POST", "cred-coder") not in profiles.posted and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ("POST", "cred-coder") in profiles.posted, "the Live Activity thread never posted with its profile"


def test_a_released_decision_is_cancelled_with_its_profile(profiles):
    _as_coder(profiles, lambda: clarify_loop._release_decision("conduit-push-abc123", relay_won=False))

    assert profiles.done.wait(5), "the release thread never ran"
    assert profiles.posted == [("DELETE", "cred-coder")]


@pytest.mark.parametrize("spawner", ["turn_end_timer", "call_thread"])
def test_timers_and_call_threads_keep_the_profile(profiles, spawner):
    seen, ran = [], threading.Event()

    def work():
        seen.append(profiles.home())
        ran.set()

    start = (lambda: plugin._later(0.0, work)) if spawner == "turn_end_timer" else (lambda: calls._spawn(work))
    _as_coder(profiles, start)

    assert ran.wait(5)
    assert seen == [profiles.coder]
