"""sealing.py: the one copy of the sealing the phone, location, health and
inbox stores share. Each store's own vector test proves its bytes still
match the phone; these prove the stores agree with each other and that each
still refuses with its own error."""

import importlib.util
import pathlib
import secrets
import sys
import tempfile
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


sealing = _load("conduit_push.sealing", ROOT / "sealing.py")
STORES = {
    "phone": (_load("conduit_push.phone_store", ROOT / "phone_store.py"), "PhoneError", "calendar sharing"),
    "location": (_load("conduit_push.location_store", ROOT / "location_store.py"), "LocationError",
                 "location sharing"),
    "health": (_load("conduit_push.health_store", ROOT / "health_store.py"), "HealthError", "health sync"),
    "inbox": (_load("conduit_push.inbox_store", ROOT / "inbox_store.py"), "InboxError", "the inbox"),
}
AAD = b"conduit-test/1\naad"


@pytest.mark.parametrize("sealer", sorted(STORES))
@pytest.mark.parametrize("opener", sorted(STORES))
def test_a_body_sealed_by_one_store_opens_in_every_other(sealer, opener):
    key = secrets.token_bytes(sealing.KEY_BYTES)
    nonce, ciphertext = STORES[sealer][0].seal(key, b"hello", AAD)
    assert STORES[opener][0].open_sealed(key, nonce, ciphertext, AAD) == b"hello"


@pytest.mark.parametrize("name", sorted(STORES))
def test_each_store_derives_with_its_own_labels(name):
    store = STORES[name][0]
    host_private, host_public = store.new_host_keypair()
    _, device_public = sealing.new_host_keypair()
    shared = store.agree(host_private, device_public)
    assert store.derive_key(shared, "phone-installation-1", device_public, host_public) == sealing.derive_key(
        shared, store.SALT, store.INFO_INGEST, "phone-installation-1", device_public, host_public)
    others = {other.SALT for other, _, _ in STORES.values() if other is not store}
    assert store.SALT not in others


@pytest.mark.parametrize("name", sorted(STORES))
def test_each_store_refuses_with_its_own_error(name):
    store, error_name, feature = STORES[name]
    error = getattr(store, error_name)
    key = secrets.token_bytes(sealing.KEY_BYTES)
    nonce, ciphertext = store.seal(key, b"hello", AAD)
    with pytest.raises(error) as wrong_key:
        store.open_sealed(secrets.token_bytes(sealing.KEY_BYTES), nonce, ciphertext, AAD)
    assert (wrong_key.value.code, wrong_key.value.status) == ("bad_seal", 409)
    assert str(wrong_key.value).endswith(f"set up {feature} again")
    with pytest.raises(error) as short_nonce:
        store.open_sealed(key, store.b64u(b"short"), ciphertext, AAD)
    assert short_nonce.value.code == "malformed"
    with pytest.raises(error) as not_base64:
        store.unb64u("not base64url!")
    assert not_base64.value.code == "malformed"
    with pytest.raises(error) as zero_key:
        store.agree(store.new_host_keypair()[0], bytes(32))
    assert zero_key.value.code == "bad_key"
