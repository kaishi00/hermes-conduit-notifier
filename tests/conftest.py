"""Shared fixtures: no test leaves Pulse's loop thread running.

Test modules stand in for the Hermes modules they need when Hermes isn't
there (``if "hermes_constants" not in sys.modules: ...``). Inside a real
Hermes install, the way Hermes' update check runs this suite, the real
modules are imported here first, before any test module is collected, so a
stand-in never shadows one (notifier #321)."""

import atexit
import os
import shutil
import sys
import tempfile

import pytest

# Never the real Hermes home: inside a Hermes install, Hermes' own
# get_hermes_home would hand the tests ~/.hermes, where a test's files (the
# workstreams registry, the calls store) would land among the user's and
# change what later tests read. Set before anything imports Hermes.
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="conduit-tests-hermes-")
atexit.register(shutil.rmtree, os.environ["HERMES_HOME"], True)
for _name in ("hermes_constants", "tools.clarify_tool", "hermes_cli.urllib_security"):
    if _name not in sys.modules:
        try:
            __import__(_name)
        except ImportError:  # no Hermes here: the test modules' stand-ins
            pass
del _name
# A home that can't be made, even by root: under a file. Tests of a Hermes
# home that isn't there use it (a path at / would be made by a root run).
_no_home_parent = os.path.join(os.environ["HERMES_HOME"], "not-a-folder")
open(_no_home_parent, "w").close()
os.environ["CONDUIT_TESTS_NO_HOME"] = os.path.join(_no_home_parent, "hermes")
del _no_home_parent


@pytest.fixture(autouse=True)
def _no_host_managed_plugin(monkeypatch):
    # A host that manages the plugin (CONDUIT_PUSH_MANAGED) changes what
    # /capabilities answers and refuses /self/update. Tests run unmanaged
    # unless one sets it, even on a host that exports it.
    monkeypatch.delenv("CONDUIT_PUSH_MANAGED", raising=False)


# The plugin's guards on Hermes' own modules. Without Hermes they patch the
# stand-ins a test made; inside a Hermes install a test that registers the
# plugin and never unloads it would leave them on the real modules, where a
# later test's stand-in finds them already installed (notifier #321).
_GUARDS = ("room_memory", "room_planner", "room_cutoff", "room_wakeups", "room_cleanup", "cron_bot_chat",
           "compaction_skip", "review_notices", "forks", "memory_scan")


def _plugin_copies(names):
    """Every loaded copy of the plugin's modules named ``names``: the package
    (conduit_push.*), the copies tests and the dashboard load under names of
    their own (conduit_push_personality.forks, conduit_push_dashboard_pulse),
    and the ones the dashboard's rooms routes hold."""
    for name, module in list(sys.modules.items()):
        if module is None or not name.startswith("conduit_push"):
            continue
        tail = name.rsplit(".", 1)[-1]
        if tail in names or any(tail.endswith("_" + wanted) for wanted in names):
            yield module
    # The dashboard's rooms routes keep their own guard copies on the module
    # (rooms._room_planner_module), outside sys.modules.
    for name, module in list(sys.modules.items()):
        if module is None or not name.endswith(".api.rooms"):
            continue
        for wanted in names:
            copy = getattr(module, f"_{wanted}_module", None)
            if copy is not None:
                yield copy


@pytest.fixture(autouse=True)
def _guards_come_off_with_the_test():
    yield
    for module in _plugin_copies(_GUARDS):
        uninstall = getattr(module, "uninstall", None)
        if callable(uninstall):
            try:
                uninstall()
            except Exception:  # noqa: BLE001 - a test's own stand-in may be gone already
                pass


@pytest.fixture(autouse=True)
def _pulse_loop_stops_with_the_test():
    yield
    # plugin.register(ctx) starts Pulse's loop (pulse.register). A test that
    # registers without an on_unload would leave it ticking, every TICK_S,
    # through every later test's monkeypatched home and host: a tick of its
    # own racing a test's tick can send or settle what the test expected
    # to find in flight.
    for pulse in _plugin_copies(("pulse",)):
        thread = getattr(pulse, "_thread", None)
        if thread is not None and getattr(thread, "is_alive", lambda: False)() and callable(getattr(pulse, "stop", None)):
            pulse.stop(timeout=2.0)
