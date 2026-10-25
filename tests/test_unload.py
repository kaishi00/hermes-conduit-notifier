"""Unloading the plugin (disable, reload) stops its background work: nothing
more goes to the relay from that load."""
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


def _ctx(unloads):
    return types.SimpleNamespace(profile_name="default", register_hook=lambda name, fn: None,
                                 register_cli_command=lambda **kwargs: None, register_tool=lambda **kwargs: None,
                                 register_skill=lambda name, path: None, on_unload=unloads.append)


@pytest.fixture
def client(tmp_path, monkeypatch):
    module = _private_copy("conduit_push._client_unload_under_test", ROOT / "client.py")
    path = tmp_path / "conduit-push.json"
    path.write_text(json.dumps({"relay_url": "https://relay.example", "credential": "c", "installation_id": "i"}))
    monkeypatch.setattr(module, "state_path", lambda: path)
    yield module
    module.stop(timeout=1.0)


@pytest.fixture
def unloaded_plugin(tmp_path, monkeypatch, client):
    """The plugin registered with a Hermes that has on_unload; returns the
    callback it registered. The plugin's modules run on fresh copies."""
    monkeypatch.setattr(plugin, "client", client)
    monkeypatch.setattr(plugin, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(calls, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(live_activity, "reporter", live_activity.LiveActivityReporter(post=lambda payload: {}))
    monkeypatch.setattr(plugin, "_pending_ready", {})
    unloads = []
    plugin.register(_ctx(unloads))
    assert unloads == [plugin._unload]
    yield unloads[0]
    calls.start()


@pytest.fixture
def room_planner(monkeypatch):
    """Hermes' room planner, as far as the plugin's guard touches it."""
    planner = types.ModuleType("gateway.hosted_room_discussion")
    planner._derive_member_watermarks = lambda events: {}
    gateway = types.ModuleType("gateway")
    gateway.__path__ = []
    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.hosted_room_discussion", planner)
    return planner


def test_the_room_planner_guard_comes_off_with_the_plugin(room_planner, unloaded_plugin):
    own = room_planner._derive_member_watermarks.__wrapped__

    unloaded_plugin()

    assert room_planner._derive_member_watermarks is own


def test_a_reply_held_for_its_turns_end_is_dropped_with_its_timer(unloaded_plugin, monkeypatch):
    sent = []
    monkeypatch.setattr(plugin, "READY_GRACE_S", 0.2)
    monkeypatch.setattr(plugin, "enqueue", sent.append)
    monkeypatch.setattr(plugin.calls, "turn_ended", lambda *args, **kwargs: False)
    monkeypatch.setattr(plugin, "_desktop_watched", lambda session_id, started: False)
    monkeypatch.setattr(plugin, "_session_title", lambda session_id: "")
    monkeypatch.setattr(plugin, "_source_notifies", lambda platform: True)
    plugin._post_llm_call(session_id="s", turn_id="t", assistant_response="Done.")
    [(_ready, _send, timer)] = plugin._pending_ready.values()

    unloaded_plugin()

    timer.join(2.0)
    assert plugin._pending_ready == {}
    assert sent == [], "the turn-end fallback still sent the reply"


def test_the_delivery_worker_drops_the_queue_and_ends(unloaded_plugin, client, monkeypatch):
    sending, release, sent = threading.Event(), threading.Event(), []

    def request_json(url, **kwargs):
        sent.append(kwargs["payload"]["event_id"])
        sending.set()
        release.wait(5)
        return {}

    monkeypatch.setattr(client, "request_json", request_json)
    for n in range(3):
        assert client.enqueue({"event_id": f"response:{n}", "type": "response.ready"})
    assert sending.wait(5)
    worker = client._worker

    # Unloaded while the first event is on its way.
    unloading = threading.Thread(target=unloaded_plugin)
    unloading.start()
    deadline = time.monotonic() + 5
    while not client._stopped.is_set() and time.monotonic() < deadline:
        time.sleep(0.01)
    with client._worker_lock:  # stop() empties the queue under it
        pass
    release.set()
    unloading.join(5)

    worker.join(2.0)
    assert not worker.is_alive()
    assert sent == ["response:0"], "events queued before the unload were still sent"
    assert client.enqueue({"event_id": "response:late", "type": "response.ready"}) is False


def test_an_unload_ends_a_delivery_waiting_to_retry(client, monkeypatch):
    attempts = []

    def unavailable(url, **kwargs):
        attempts.append(kwargs["payload"]["event_id"])
        raise client.RelayRejected(503, "unavailable")

    monkeypatch.setattr(client, "request_json", unavailable)
    assert client.enqueue({"event_id": "response:1", "type": "response.ready"})
    deadline = time.monotonic() + 5
    while not attempts and time.monotonic() < deadline:
        time.sleep(0.01)
    worker = client._worker

    started = time.monotonic()
    client.stop(timeout=5.0)

    assert not worker.is_alive()
    assert time.monotonic() - started < 2.0, "the retry wait outlived the unload"
    assert attempts == ["response:1"]


def test_a_plugin_enabled_again_delivers_again(client, monkeypatch):
    sent = []
    monkeypatch.setattr(client, "request_json", lambda url, **kwargs: sent.append(kwargs["payload"]["event_id"]) or {})
    assert client.enqueue({"event_id": "response:1", "type": "response.ready"})
    client._events.join()
    client.stop()
    client.start()

    assert client.enqueue({"event_id": "response:2", "type": "response.ready"})
    client._events.join()

    assert sent == ["response:1", "response:2"]


def test_a_held_calls_waiter_ends_without_calling(unloaded_plugin, monkeypatch):
    fired = []
    store = types.SimpleNamespace(next_due=lambda: time.time() + 60, clock=time.time,
                                  fire_due=lambda: fired.append(True) or [])
    monkeypatch.setattr(calls, "_store", lambda home: store)
    calls._wait_for_holds("home-1", "default")
    [waiter] = [thread for thread in threading.enumerate() if thread.name == "conduit-call"]

    unloaded_plugin()

    waiter.join(2.0)
    assert not waiter.is_alive()
    assert fired == [], "a held call went out after the unload"
    assert calls._waiting == set()
    # Nothing new waits until the plugin is loaded again.
    calls._wait_for_holds("home-1", "default")
    assert calls._waiting == set()


def test_the_live_activity_thread_ends(unloaded_plugin):
    reporter = live_activity.reporter
    reporter.turn_started("s1")
    thread = reporter._thread
    assert thread is not None and thread.is_alive()

    unloaded_plugin()

    thread.join(2.0)
    assert not thread.is_alive()
    reporter.turn_started("s2")
    assert reporter._thread is None
    reporter.start()
    reporter.turn_started("s3")
    assert reporter._thread is not None and reporter._thread.is_alive()
    reporter.stop()


def test_a_hermes_without_on_unload_still_loads_the_plugin(monkeypatch, tmp_path):
    monkeypatch.setattr(calls, "get_hermes_home", lambda: tmp_path)
    ctx = _ctx([])
    del ctx.on_unload
    plugin.register(ctx)
