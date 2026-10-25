"""The end-to-end sealing the phone, location, health and inbox stores share.

Each of those features agrees its own key with this profile per device
(X25519), derives the keys it needs with HKDF-SHA256 and seals bodies with
ChaCha20-Poly1305 under its own labels (salt, info, AAD tag, key-id tag).
The labels stay in each store; the primitives live here once, so a change
to key handling reaches every feature together.

A store passes its own error class (``error``), called as
``error(code, message, status=...)``, so the dashboard routes keep
answering each feature's refusals as they did. The stores import this
module relatively, so the dashboard loads them as a private package
(``_plugin_package_module``) rather than by path. Nothing here imports the
rest of the plugin.

Notification sealing (e2e.py) is a different scheme with its own pairing
secret and errors, and stays separate.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from typing import Any, Callable

NONCE_BYTES = 12
KEY_BYTES = 32
TAG_BYTES = 16

_B64URL = re.compile(r"^[A-Za-z0-9_-]*\Z")

# error(code, message, status=...) -> Exception
ErrorFactory = Callable[..., Exception]


def b64u(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def unb64u(value: Any, error: ErrorFactory) -> bytes:
    if not isinstance(value, str) or not _B64URL.match(value) or len(value) % 4 == 1:
        raise error("malformed", "malformed base64url")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def crypto_available() -> bool:
    try:
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey  # noqa: F401
        from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305  # noqa: F401
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF  # noqa: F401
    except Exception:
        return False
    return True


def key_id(kid_tag: bytes, device_public: bytes, host_public: bytes) -> str:
    return hashlib.sha256(kid_tag + device_public + host_public).hexdigest()[:32]


def derive_key(shared: bytes, salt: bytes, purpose: bytes, device_id: str, device_public: bytes,
               host_public: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    info = purpose + b"\n" + device_id.encode("ascii") + b"\n" + device_public + host_public
    return HKDF(algorithm=hashes.SHA256(), length=KEY_BYTES, salt=salt, info=info).derive(shared)


def agree(host_private: bytes, device_public: bytes, error: ErrorFactory) -> bytes:
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey

    try:
        shared = X25519PrivateKey.from_private_bytes(host_private).exchange(
            X25519PublicKey.from_public_bytes(device_public))
    except ValueError:  # OpenSSL refuses an all-zero result itself
        shared = bytes(32)
    if shared == bytes(32):
        raise error("bad_key", "That public key can't be used")
    return shared


def new_host_keypair() -> tuple[bytes, bytes]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    private = X25519PrivateKey.generate()
    raw = serialization.Encoding.Raw
    return (
        private.private_bytes(raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()),
        private.public_key().public_bytes(raw, serialization.PublicFormat.Raw),
    )


def seal(key: bytes, plaintext: bytes, aad: bytes, nonce: bytes | None = None) -> tuple[str, str]:
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    nonce = nonce if nonce is not None else secrets.token_bytes(NONCE_BYTES)
    return b64u(nonce), b64u(ChaCha20Poly1305(key).encrypt(nonce, plaintext, aad))


def open_sealed(key: bytes, nonce: Any, ciphertext: Any, aad: bytes, error: ErrorFactory, set_up_again: str) -> bytes:
    """``set_up_again`` finishes the refusal when the key doesn't open the
    body, e.g. "set up location sharing again"."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    raw_nonce = unb64u(nonce, error)
    if len(raw_nonce) != NONCE_BYTES:
        raise error("malformed", "nonce must be 12 bytes")
    raw_ciphertext = unb64u(ciphertext, error)
    if len(raw_ciphertext) < TAG_BYTES:
        raise error("malformed", "ct is shorter than its tag")
    try:
        return ChaCha20Poly1305(key).decrypt(raw_nonce, raw_ciphertext, aad)
    except InvalidTag:
        raise error("bad_seal", f"The body didn't open with this device's key; {set_up_again}",
                    status=409) from None
