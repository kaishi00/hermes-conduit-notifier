"""Hermes never parses the plugin's code to ask whether it is a memory
provider (memory_scan, hermes-conduit#1931)."""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def scan(monkeypatch):
    """memory_scan, loaded fresh, beside a stand-in for Hermes' plugins.memory
    whose parse records the folders it was asked about
    (upstream/test_memory_scan.py holds the real one to it)."""
    memory = types.ModuleType("plugins.memory")
    asked = []

    def defines(path):
        asked.append(Path(path))
        return Path(path).name == "provider"

    memory._defines_memory_provider = defines
    plugins = types.ModuleType("plugins")
    plugins.memory = memory
    monkeypatch.setitem(sys.modules, "plugins", plugins)
    monkeypatch.setitem(sys.modules, "plugins.memory", memory)
    monkeypatch.delitem(sys.modules, "conduit_push_memory_scan", raising=False)
    spec = importlib.util.spec_from_file_location("conduit_push_memory_scan_test", ROOT / "memory_scan.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.memory, module.own, module.asked = memory, defines, asked
    yield module
    module.uninstall()


def test_the_plugins_own_folder_is_not_parsed(scan, tmp_path):
    assert scan.install()
    check = scan.memory._defines_memory_provider
    assert check(ROOT) is False
    assert check(Path(str(ROOT) + "/.")) is False  # Hermes passes the folder as it lists it
    assert scan.asked == []
    # Every other folder is still Hermes' to judge.
    assert check(tmp_path / "provider") is True
    assert check(tmp_path / "other") is False
    assert scan.asked == [tmp_path / "provider", tmp_path / "other"]


def test_a_second_copy_adds_its_folder_to_the_one_guard(scan, tmp_path):
    assert scan.install()
    guard = scan.memory._defines_memory_provider
    copy = tmp_path / "conduit_push"
    copy.mkdir()
    scan._wrapped_module = None  # as a fresh load of the module would start
    scan.HERE = copy.resolve()
    assert scan.install()
    # The second load wraps Hermes' own function, not the first load's guard.
    assert scan.memory._defines_memory_provider.__wrapped__ is scan.own
    assert scan.memory._defines_memory_provider is not guard
    assert scan.memory._defines_memory_provider(copy) is False
    assert scan.memory._defines_memory_provider(ROOT) is False
    assert scan.asked == []


def test_the_later_copy_unloading_first_leaves_the_earlier_one_guarded(scan, tmp_path):
    assert scan.install()
    first = scan.HERE
    scan._wrapped_module = None
    copy = tmp_path / "conduit_push"
    copy.mkdir()
    scan.HERE = copy.resolve()
    assert scan.install()
    scan.uninstall()  # the later copy unloads
    assert scan.memory._defines_memory_provider(ROOT) is False
    assert scan.asked == []
    # The unloaded copy's folder is Hermes' to judge again.
    scan.memory._defines_memory_provider(copy)
    assert scan.asked == [copy]
    scan.HERE = first
    scan._wrapped_module = scan.memory
    scan.uninstall()  # the earlier copy unloads last: Hermes' own is back
    assert scan.memory._defines_memory_provider is scan.own


def test_unload_puts_hermes_own_back(scan):
    assert scan.install()
    scan.uninstall()
    assert scan.memory._defines_memory_provider is scan.own


def test_a_hermes_without_the_parse_is_left_alone(scan):
    del scan.memory._defines_memory_provider
    assert scan.install() is False
    assert not hasattr(scan.memory, "_defines_memory_provider")
