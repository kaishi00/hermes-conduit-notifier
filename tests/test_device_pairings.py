"""Pairings never let one device overwrite or borrow another's credentials."""
import hashlib
import importlib.util
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _module():
    spec = importlib.util.spec_from_file_location("conduit_device_pairings_test", ROOT / "device_pairings.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(path, installation="phone", **extra):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"credential": "credential", "installation_id": installation, **extra}))


def _sidecar(primary, installation):
    return primary.parent / "conduit-push-devices" / (hashlib.sha256(installation.encode()).hexdigest() + ".json")


def test_storage_enumerates_sidecars_even_without_a_primary(tmp_path):
    primary = tmp_path / "conduit-push.json"
    sidecar = _sidecar(primary, "tablet")
    _write(sidecar, "tablet")
    module = _module()
    assert module.paths(primary) == [sidecar]
    assert module.load(sidecar)["installation_id"] == "tablet"
    assert module.for_installation(primary, "tablet") == sidecar
    assert module.for_installation(primary, "../missing") == _sidecar(primary, "../missing")
    absent = tmp_path / "absent" / "conduit-push.json"
    module.for_installation(absent, "phone")
    assert not absent.parent.exists(), "status reads must not create directories"


def test_storage_ignores_symlinks_bad_credentials_duplicates_and_wrong_filenames(tmp_path):
    primary = tmp_path / "conduit-push.json"
    _write(primary)
    duplicate = _sidecar(primary, "phone")
    _write(duplicate)
    tablet = _sidecar(primary, "tablet")
    _write(tablet, "tablet")
    _write(_sidecar(primary, "wrong"), "different")
    _write(_sidecar(primary, "invalid"), "invalid", credential={"secret": "not a string"})
    _write(tablet.with_name("arbitrary.json"), "other")
    target = tmp_path / "outside.json"
    _write(target, "linked")
    linked = _sidecar(primary, "linked")
    linked.symlink_to(target)
    module = _module()
    assert module.paths(primary) == [primary, tablet]
    assert module.for_installation(primary, "phone") == primary
    assert module.load(linked) is None
    primary.unlink()
    primary.symlink_to(target)
    assert module.load(primary) is None
    assert primary not in module.paths(primary)


def test_storage_does_not_follow_a_symlinked_sidecar_directory(tmp_path):
    primary = tmp_path / "conduit-push.json"
    external = tmp_path / "external"
    external.mkdir()
    (tmp_path / "conduit-push-devices").symlink_to(external, target_is_directory=True)
    _write(_sidecar(primary, "tablet"), "tablet")
    assert _module().paths(primary) == []
