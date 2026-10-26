"""Each installation's identity for Linked Rooms (designs/cross-owner-agents, decision 4)."""
import base64
import hashlib
import importlib.util
import json
import os
import pathlib
import stat
import sys
import tempfile
import threading
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

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


identity_module = _load("conduit_push.room_identity", ROOT / "room_identity.py")

# The BIP39 English list as published (bitcoin/bips, bip-0039/english.txt).
BIP39_ENGLISH_SHA256 = "2f5eed53a4727b4bf8880d8f3f199efc90e58503646d9ff8eff3a2ed3b24dbda"


def _b64u(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def test_an_identity_is_made_once_and_kept(tmp_path):
    made = identity_module.load_or_create(tmp_path)
    again = identity_module.load_or_create(tmp_path)

    assert again.signing_public == made.signing_public
    assert again.sealing_public == made.sealing_public
    assert len(made.signing_public) == 32 and len(made.sealing_public) == 32
    assert made.signing_public != made.sealing_public
    path = tmp_path / "conduit-rooms" / "identity.json"
    assert _mode(path) == 0o600
    assert _mode(path.parent) == 0o700
    stored = json.loads(path.read_text())
    assert stored["version"] == 1
    assert set(stored) == {"version", "sign_key", "seal_key", "created_at"}
    assert identity_module.load(tmp_path).signing_public == made.signing_public


def test_the_inbox_is_named_after_the_signing_key_as_the_mailbox_names_it(tmp_path):
    made = identity_module.load_or_create(tmp_path)

    assert made.inbox == _b64u(hashlib.sha256(made.signing_public).digest())
    assert identity_module.inbox_id(made.signing_public) == made.inbox
    assert len(made.inbox) == 43


def test_no_identity_until_one_is_made(tmp_path):
    assert identity_module.load(tmp_path) is None
    assert not (tmp_path / "conduit-rooms").exists()


def test_two_threads_make_one_identity(tmp_path):
    start = threading.Barrier(2)
    made = []

    def make():
        start.wait()
        made.append(identity_module.load_or_create(tmp_path))

    threads = [threading.Thread(target=make) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(made) == 2
    assert made[0].signing_public == made[1].signing_public


def test_a_file_it_cant_parse_is_refused_and_kept(tmp_path):
    path = tmp_path / "conduit-rooms" / "identity.json"
    path.parent.mkdir()
    path.write_text("{not json")

    with pytest.raises(identity_module.IdentityError) as refused:
        identity_module.load_or_create(tmp_path)

    assert refused.value.code == "unreadable"
    assert "every link" in str(refused.value)
    assert path.read_text() == "{not json"
    with pytest.raises(identity_module.IdentityError):
        identity_module.load(tmp_path)


@pytest.mark.parametrize("stored", [
    {"version": 1, "sign_key": "AAAA", "seal_key": _b64u(bytes(32)), "created_at": 1},
    {"version": 1, "sign_key": _b64u(bytes(32)), "created_at": 1},
    {"version": 2, "sign_key": _b64u(bytes(32)), "seal_key": _b64u(bytes(32)), "created_at": 1},
    {"version": True, "sign_key": _b64u(bytes(32)), "seal_key": _b64u(bytes(32)), "created_at": 1},
    {"version": 1.0, "sign_key": _b64u(bytes(32)), "seal_key": _b64u(bytes(32)), "created_at": 1},
    ["not", "an", "object"],
])
def test_keys_of_a_shape_it_doesnt_write_are_refused_and_kept(tmp_path, stored):
    path = tmp_path / "conduit-rooms" / "identity.json"
    path.parent.mkdir()
    path.write_text(json.dumps(stored))

    with pytest.raises(identity_module.IdentityError) as refused:
        identity_module.load_or_create(tmp_path)

    assert refused.value.code == "unreadable"
    assert json.loads(path.read_text()) == stored


def test_without_cryptography_it_says_so_whether_or_not_theres_an_identity(tmp_path, monkeypatch):
    identity_module.load_or_create(tmp_path / "made")
    monkeypatch.setattr(identity_module.sealing, "crypto_available", lambda: False)

    for root, reads in ((tmp_path / "made", (identity_module.load, identity_module.load_or_create)),
                        (tmp_path / "none", (identity_module.load_or_create,))):
        for read in reads:
            with pytest.raises(identity_module.IdentityError) as refused:
                read(root)
            assert refused.value.code == "no_crypto"
    assert identity_module.load(tmp_path / "none") is None
    assert not (tmp_path / "none").exists()


def test_the_file_is_owner_only_whatever_the_umask(tmp_path):
    previous = os.umask(0)
    try:
        identity_module.load_or_create(tmp_path)
    finally:
        os.umask(previous)

    assert _mode(tmp_path / "conduit-rooms" / "identity.json") == 0o600


@pytest.mark.skipif(os.name != "posix", reason="a directory is flushed where the system can open one")
def test_a_new_identity_is_on_disk_before_its_used(tmp_path, monkeypatch):
    # After a crash the identity a Link was made with is the one there,
    # never none (and so, silently, a new one): the new directory's entry,
    # the file and its rename are each flushed, in that order.
    flushed, real_fsync, real_replace = [], os.fsync, pathlib.Path.replace

    def fsync(descriptor):
        flushed.append(os.fstat(descriptor).st_ino)
        real_fsync(descriptor)

    def replace(self, target):
        moved = real_replace(self, target)
        flushed.append("renamed")
        return moved

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(pathlib.Path, "replace", replace)
    identity_module.load_or_create(tmp_path)

    directory = tmp_path / "conduit-rooms"
    root, rooms = os.stat(tmp_path).st_ino, os.stat(directory).st_ino
    file = os.stat(directory / "identity.json").st_ino
    assert flushed.index(root) < flushed.index(file) < flushed.index("renamed") < flushed.index(rooms)


def test_its_repr_holds_no_private_key(tmp_path):
    made = identity_module.load_or_create(tmp_path)
    stored = json.loads((tmp_path / "conduit-rooms" / "identity.json").read_text())

    shown = repr(made) + str(made)
    assert stored["sign_key"] not in shown and stored["seal_key"] not in shown
    assert repr(made.signing_key) not in shown and repr(made.sealing_key) not in shown


def test_a_signature_verifies_only_for_its_message_and_key(tmp_path):
    made = identity_module.load_or_create(tmp_path)
    other = identity_module.load_or_create(tmp_path / "other")
    signature = made.sign(b"hello")

    assert identity_module.verify(made.signing_public, b"hello", signature)
    assert not identity_module.verify(made.signing_public, b"hello!", signature)
    assert not identity_module.verify(other.signing_public, b"hello", signature)
    assert not identity_module.verify(made.signing_public, b"hello", signature[:-1])
    assert not identity_module.verify(b"short", b"hello", signature)


def test_the_word_list_is_bip39_english():
    data = (ROOT / "bip39_english.txt").read_bytes()

    assert hashlib.sha256(data).hexdigest() == BIP39_ENGLISH_SHA256
    assert len(identity_module.WORDS) == 2048
    assert identity_module.WORDS[0] == "abandon" and identity_module.WORDS[-1] == "zoo"


def test_fingerprint_words_known_vector():
    sign_public = bytes(range(32))
    seal_public = bytes(range(32, 64))

    words = identity_module.fingerprint_words(sign_public, seal_public)

    digest = hashlib.sha256(b"conduit-rooms-fingerprint-v1\n" + sign_public + seal_public).digest()
    bits = int.from_bytes(digest[:6], "big") >> 4
    expected = tuple(identity_module.WORDS[(bits >> shift) & 0x7FF] for shift in (33, 22, 11, 0))
    assert words == expected
    assert words == ("tail", "slogan", "illegal", "charge")


def test_fingerprint_words_follow_both_keys(tmp_path):
    made = identity_module.load_or_create(tmp_path)

    assert made.fingerprint == identity_module.fingerprint_words(made.signing_public, made.sealing_public)
    assert len(made.fingerprint) == 4
    assert identity_module.fingerprint_words(made.signing_public, bytes(32)) != made.fingerprint


def test_the_root_home_of_a_profile_home(tmp_path, monkeypatch):
    monkeypatch.delattr(sys.modules["hermes_constants"], "get_default_hermes_root", raising=False)
    (tmp_path / "profiles" / "work").mkdir(parents=True)

    assert identity_module.root_home(tmp_path / "profiles" / "work") == tmp_path
    assert identity_module.root_home(tmp_path) == tmp_path


def test_the_root_home_is_hermes_own_where_it_says(tmp_path, monkeypatch):
    asked = []

    def get_default_hermes_root(*, home=None):
        asked.append(home)
        return tmp_path / "root"

    monkeypatch.setattr(sys.modules["hermes_constants"], "get_default_hermes_root", get_default_hermes_root,
                        raising=False)

    assert identity_module.root_home(tmp_path / "somewhere") == tmp_path / "root"
    assert asked == [tmp_path / "somewhere"]
