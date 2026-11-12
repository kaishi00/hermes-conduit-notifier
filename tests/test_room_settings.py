"""The host's room settings (room_settings.py, notifier #443)."""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def settings(monkeypatch):
    spec = importlib.util.spec_from_file_location("conduit_room_settings_test", ROOT / "room_settings.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # The file as written, whatever Hermes this runs beside.
    monkeypatch.setitem(sys.modules, "hermes_cli.config_effective", None)
    return module


def _write(root, text):
    root.mkdir(parents=True, exist_ok=True)
    path = root / "config.yaml"
    path.write_text("plugins:\n  entries:\n    conduit_push:\n      settings:\n" + text, encoding="utf-8")
    return path


def test_the_defaults_keep_what_the_plugin_did(settings, tmp_path):
    assert settings.cleanup_disbanded(tmp_path) == "delete"
    assert settings.conduit_rooms_only(tmp_path) is False
    assert settings.applies("desktop-room", tmp_path)


def test_a_profile_reads_the_roots_settings(settings, tmp_path):
    _write(tmp_path, "        cleanup_disbanded_rooms: 'off'\n        conduit_rooms_only: true\n")
    profile = tmp_path / "profiles" / "furina"
    profile.mkdir(parents=True)
    _write(profile, "        cleanup_disbanded_rooms: delete\n")

    assert settings.cleanup_disbanded(profile) == "off"
    assert settings.conduit_rooms_only(profile) is True
    assert not settings.applies("desktop-room", profile)
    # Conduit's rooms, and a room whose id isn't known yet, keep the plugin's changes.
    assert settings.applies("conduit-0f8e", profile)
    assert settings.applies("", profile) and settings.applies(None, profile)


def test_a_change_holds_from_the_next_read(settings, tmp_path):
    path = _write(tmp_path, "        conduit_rooms_only: false\n")
    assert settings.conduit_rooms_only(tmp_path) is False
    _write(tmp_path, "        conduit_rooms_only: true \n")
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert settings.conduit_rooms_only(tmp_path) is True


def test_what_it_cant_read_is_the_default_logged_once(settings, tmp_path, caplog):
    _write(tmp_path, "        cleanup_disbanded_rooms: archive\n        conduit_rooms_only: sometimes\n")
    for _ in range(2):
        assert settings.cleanup_disbanded(tmp_path) == "delete"
        assert settings.conduit_rooms_only(tmp_path) is False
    assert caplog.text.count("cleanup_disbanded_rooms is 'archive'") == 1
    assert caplog.text.count("conduit_rooms_only is 'sometimes'") == 1

    settings._clear()
    (tmp_path / "config.yaml").write_text("plugins: [unclosed\n", encoding="utf-8")
    assert settings.cleanup_disbanded(tmp_path) == "delete"
    assert "couldn't read the room settings" in caplog.text


def test_the_manifest_declares_both_for_hermes_plugins_page():
    import yaml

    schema = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))["config_schema"]
    assert schema["cleanup_disbanded_rooms"]["choices"] == ["delete", "off"]
    assert schema["cleanup_disbanded_rooms"]["default"] == "delete"
    assert schema["conduit_rooms_only"] == {**schema["conduit_rooms_only"], "type": "bool", "default": False}
