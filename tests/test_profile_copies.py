"""Every profile that turns the plugin on runs the main install (profile_copies.py, hermes-conduit#1538)."""
import importlib.util
import os
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("conduit_push_test_profile_copies", ROOT / "profile_copies.py")
profile_copies = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(profile_copies)


def _plugin(path, version, name="conduit_push"):
    path.mkdir(parents=True)
    (path / "plugin.yaml").write_text(f"name: {name}\nversion: {version}\ndescription: test\n")
    (path / "__init__.py").write_text(f"VERSION = {version!r}\n")
    return path


def _profile(root, name, config):
    home = root / "profiles" / name
    home.mkdir(parents=True)
    if config is not None:
        (home / "config.yaml").write_text(config)
    return home


@pytest.fixture
def host(tmp_path):
    root = tmp_path / ".hermes"
    shared = _plugin(root / "plugins" / "conduit_push", "0.123.0")
    return root, shared


def test_a_cloned_profiles_old_copy_is_backed_up_and_linked(host):
    root, shared = host
    home = _profile(root, "nahida", "plugins:\n  enabled: [conduit_push, hindsight]\n  disabled: [cadu-device]\n")
    _plugin(home / "plugins" / "conduit-push", "0.1.0")
    _plugin(home / "plugins" / "hindsight", "1.2.3", name="hindsight")

    changes = profile_copies.sweep(root, shared)

    link = home / "plugins" / "conduit_push"
    assert link.is_symlink() and link.resolve() == shared.resolve()
    assert not (home / "plugins" / "conduit-push").exists()
    backup = home / "plugins-backup" / "conduit-push-0.1.0"
    assert (backup / "plugin.yaml").read_text().startswith("name: conduit_push\nversion: 0.1.0")
    # Another plugin in the folder is left alone.
    assert (home / "plugins" / "hindsight" / "plugin.yaml").exists()
    assert changes == ["profile nahida: moved its own copy (0.1.0) to plugins-backup/conduit-push-0.1.0",
                       "profile nahida: linked to the main install (0.123.0)"]
    # Once linked, nothing more to do.
    assert profile_copies.sweep(root, shared) == []


def test_a_profile_that_enables_the_plugin_without_a_copy_is_linked(host):
    root, shared = host
    home = _profile(root, "coder", "plugins:\n  enabled:\n    - conduit_push\n")
    assert profile_copies.sweep(root, shared) == ["profile coder: linked to the main install (0.123.0)"]
    assert (home / "plugins" / "conduit_push").resolve() == shared.resolve()


def test_profiles_that_dont_turn_it_on_are_left_alone(host):
    root, shared = host
    off = _profile(root, "rei", "plugins:\n  enabled: []\n")
    disabled = _profile(root, "zhongli", "plugins:\n  enabled: [conduit_push]\n  disabled: [conduit_push]\n")
    _plugin(disabled / "plugins" / "conduit-push", "0.1.0")
    unlisted = _profile(root, "raiden", None)
    assert profile_copies.sweep(root, shared) == []
    assert not (off / "plugins").exists()
    assert (disabled / "plugins" / "conduit-push" / "plugin.yaml").exists()
    assert not (unlisted / "plugins").exists()


def test_without_an_allow_list_only_a_profile_with_a_copy_counts(host):
    root, shared = host
    home = _profile(root, "kept", "model: x\n")
    _plugin(home / "plugins" / "conduit_push", "0.50.0")
    _profile(root, "bare", "model: x\n")
    assert profile_copies.sweep(root, shared) == [
        "profile kept: moved its own copy (0.50.0) to plugins-backup/conduit_push-0.50.0",
        "profile kept: linked to the main install (0.123.0)"]


def test_a_second_backup_of_the_same_version_gets_its_own_name(host):
    root, shared = host
    home = _profile(root, "nahida", "plugins:\n  enabled: [conduit_push]\n")
    _plugin(home / "plugins-backup" / "conduit-push-0.1.0", "0.1.0")
    _plugin(home / "plugins" / "conduit-push", "0.1.0")
    profile_copies.sweep(root, shared)
    assert (home / "plugins-backup" / "conduit-push-0.1.0-2" / "plugin.yaml").exists()


def test_where_no_link_can_be_made_the_main_install_is_copied_and_kept_current(host):
    root, shared = host
    home = _profile(root, "nahida", "plugins:\n  enabled: [conduit_push]\n")
    (shared / ".git").mkdir()

    def refuse(_source, _target):
        raise OSError("symbolic links need privileges here")

    assert profile_copies.sweep(root, shared, link=refuse) == [
        "profile nahida: copied the main install (0.123.0), as no link could be made"]
    copy = home / "plugins" / "conduit_push"
    assert not copy.is_symlink() and (copy / profile_copies.COPY_MARKER).exists()
    assert not (copy / ".git").exists()
    assert profile_copies.sweep(root, shared, link=refuse) == []
    # An update moves the main install ahead: the copy is ours, so it's replaced, not backed up.
    (shared / "plugin.yaml").write_text("name: conduit_push\nversion: 0.124.0\n")
    assert profile_copies.sweep(root, shared, link=refuse) == [
        "profile nahida: removed its outdated copy of the main install",
        "profile nahida: copied the main install (0.124.0), as no link could be made"]
    assert "0.124.0" in (copy / "plugin.yaml").read_text()
    assert not (home / "plugins-backup").exists()


def test_a_name_taken_by_something_else_is_left_alone(host):
    root, shared = host
    home = _profile(root, "nahida", "plugins:\n  enabled: [conduit_push]\n")
    elsewhere = _plugin(root / "elsewhere" / "conduit_push", "0.123.0")
    (home / "plugins").mkdir()
    os.symlink(elsewhere, home / "plugins" / "conduit_push")
    assert profile_copies.sweep(root, shared) == []
    assert (home / "plugins" / "conduit_push").resolve() == elsewhere.resolve()


def test_only_the_main_install_is_shared(host):
    root, _ = host
    home = _profile(root, "nahida", "plugins:\n  enabled: [conduit_push]\n")
    own = _plugin(home / "plugins" / "conduit-push", "0.1.0")
    # A dashboard running a profile's own copy points nobody at it.
    assert profile_copies.sweep(root, own) == []
    assert (own / "plugin.yaml").exists()


def test_one_profile_that_fails_doesnt_stop_the_others(host, monkeypatch):
    root, shared = host
    _profile(root, "a", "plugins:\n  enabled: [conduit_push]\n")
    b = _profile(root, "b", "plugins:\n  enabled: [conduit_push]\n")
    real = profile_copies._point_profile

    def flaky(home, *args):
        if home.name == "a":
            raise PermissionError("read-only")
        return real(home, *args)

    monkeypatch.setattr(profile_copies, "_point_profile", flaky)
    assert profile_copies.sweep(root, shared) == ["profile b: linked to the main install (0.123.0)"]
    assert (b / "plugins" / "conduit_push").is_symlink()
