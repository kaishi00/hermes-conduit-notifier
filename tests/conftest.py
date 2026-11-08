"""Shared fixtures: no test leaves Pulse's loop thread running."""

import sys

import pytest


@pytest.fixture(autouse=True)
def _no_host_managed_plugin(monkeypatch):
    # A host that manages the plugin (CONDUIT_PUSH_MANAGED) changes what
    # /capabilities answers and refuses /self/update. Tests run unmanaged
    # unless one sets it, even on a host that exports it.
    monkeypatch.delenv("CONDUIT_PUSH_MANAGED", raising=False)


@pytest.fixture(autouse=True)
def _pulse_loop_stops_with_the_test():
    yield
    # plugin.register(ctx) starts Pulse's loop (pulse.register). A test that
    # registers without an on_unload would leave it ticking, every TICK_S,
    # through every later test's monkeypatched home and host: a tick of its
    # own racing a test's tick can send or settle what the test expected
    # to find in flight.
    pulse = sys.modules.get("conduit_push.pulse")
    thread = getattr(pulse, "_thread", None)
    if thread is not None and thread.is_alive():
        pulse.stop(timeout=2.0)
