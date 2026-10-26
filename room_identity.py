"""This installation's identity between owners, for Linked Rooms (designs/cross-owner-agents, decision 4).

Each Hermes installation gets one long-term Ed25519 signing key and one X25519
sealing key, kept in the root home's ``conduit-rooms/identity.json`` (owner
only). The rooms mailbox names the installation's inbox after the signing
key, linked installations seal what they send to the sealing key, and both
people's Conduits show four fingerprint words made from the two keys, for an
optional check in person. Every profile of the installation shares it: Links
are per installation (review focus 5).

The file is made once and never replaced. A new identity would end every
Link, so a file this module can't read is refused with what to do, never
written over. Nothing here imports the rest of the plugin but sealing.py, so
the dashboard loads it as a private package.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]
try:
    import msvcrt
except ImportError:  # everything but Windows
    msvcrt = None  # type: ignore[assignment]

from . import sealing

DIRECTORY = "conduit-rooms"
FILE_NAME = "identity.json"
VERSION = 1
KEY_BYTES = 32
SIGNATURE_BYTES = 64

# The BIP39 English list, as published (bitcoin/bips, bip-0039/english.txt):
# 2048 short words that don't share their first four letters.
WORD_LIST = "bip39_english.txt"
WORD_LIST_SHA256 = "2f5eed53a4727b4bf8880d8f3f199efc90e58503646d9ff8eff3a2ed3b24dbda"
FINGERPRINT_LABEL = b"conduit-rooms-fingerprint-v1\n"

_KEY = re.compile(r"^[A-Za-z0-9_-]{43}\Z")
_NO_CRYPTO = "Linked Rooms needs Python's cryptography package, which this Hermes doesn't have"
_thread_lock = threading.Lock()


class IdentityError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _load_words() -> tuple[str, ...]:
    try:
        data = (Path(__file__).resolve().parent / WORD_LIST).read_bytes()
    except OSError:
        return ()
    if hashlib.sha256(data).hexdigest() != WORD_LIST_SHA256:
        return ()
    return tuple(data.decode("ascii").split())


WORDS = _load_words()


@dataclass(frozen=True)
class Identity:
    signing_key: bytes = field(repr=False)
    sealing_key: bytes = field(repr=False)
    signing_public: bytes
    sealing_public: bytes
    created_at: int

    def __repr__(self) -> str:
        return f"Identity(inbox={self.inbox!r}, created_at={self.created_at})"

    __str__ = __repr__

    @property
    def inbox(self) -> str:
        return inbox_id(self.signing_public)

    @property
    def fingerprint(self) -> tuple[str, ...]:
        return fingerprint_words(self.signing_public, self.sealing_public)

    def sign(self, message: bytes) -> bytes:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        return Ed25519PrivateKey.from_private_bytes(self.signing_key).sign(bytes(message))

    def public_keys(self) -> dict[str, str]:
        return {"sign": sealing.b64u(self.signing_public), "seal": sealing.b64u(self.sealing_public)}


def inbox_id(sign_public: bytes) -> str:
    """The mailbox's name for the inbox of the installation with this
    signing key: base64url (no padding) SHA-256 of the raw key."""
    return sealing.b64u(hashlib.sha256(bytes(sign_public)).digest())


def fingerprint_words(sign_public: bytes, seal_public: bytes) -> tuple[str, ...]:
    """Four words for both public keys: the first 44 bits of SHA-256 over a
    label and the keys, 11 bits a word."""
    if len(WORDS) != 2048:
        raise IdentityError("no_word_list", f"This plugin install is missing {WORD_LIST}, or has a changed copy; "
                                            "update the plugin")
    digest = hashlib.sha256(FINGERPRINT_LABEL + bytes(sign_public) + bytes(seal_public)).digest()
    bits = int.from_bytes(digest[:6], "big") >> 4
    return tuple(WORDS[(bits >> shift) & 0x7FF] for shift in (33, 22, 11, 0))


def verify(sign_public: bytes, message: bytes, signature: bytes) -> bool:
    """Whether ``signature`` is the Ed25519 signature of ``message`` by
    ``sign_public``. Never raises for keys or signatures of the wrong size."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if len(sign_public) != KEY_BYTES or len(signature) != SIGNATURE_BYTES:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(bytes(sign_public)).verify(bytes(signature), bytes(message))
    except (InvalidSignature, ValueError):
        return False
    return True


def root_home(home: Any = None) -> Path:
    """The root Hermes home of ``home`` (this process's when None), where
    the installation's identity lives: Hermes' own answer where it has one
    (9e01b44 and later), else the home with ``profiles/<name>`` taken off."""
    import hermes_constants

    if home is None:
        home = hermes_constants.get_hermes_home()
    home = Path(home)
    root_of = getattr(hermes_constants, "get_default_hermes_root", None)
    if callable(root_of):
        try:
            return Path(root_of(home=home))
        except Exception:  # noqa: BLE001 - a Hermes that answers oddly gets the plain rule
            pass
    if home.parent.name == "profiles":
        return home.parent.parent
    return home


def load(root: Any) -> Optional[Identity]:
    """The installation's identity, or None before one is made. A file this
    module can't read or didn't write raises IdentityError, as does any file
    on a Hermes without the cryptography package."""
    path = Path(root) / DIRECTORY / FILE_NAME
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise IdentityError("unreadable", f"Couldn't read {path} ({exc.strerror or exc}), so Linked Rooms is off on "
                                          "this installation until it can") from None
    if not sealing.crypto_available():
        raise IdentityError("no_crypto", _NO_CRYPTO)
    try:
        stored = json.loads(raw)
    except (ValueError, RecursionError):  # bytes that aren't UTF-8 included
        stored = None
    identity = _identity_from(stored)
    if identity is None:
        raise IdentityError("unreadable", _unreadable(path))
    return identity


def load_or_create(root: Any, clock: Any = time.time) -> Identity:
    """The installation's identity, made the first time it's asked for."""
    if not sealing.crypto_available():
        raise IdentityError("no_crypto", _NO_CRYPTO)
    found = load(root)
    if found is not None:
        return found
    directory = Path(root) / DIRECTORY
    made = not directory.is_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    if made:
        _sync_directory(directory.parent)
    lock_path = directory / f".{FILE_NAME}.lock"
    with _thread_lock, open(lock_path, "a+b") as handle, _exclusive(handle):
        found = load(root)
        if found is not None:
            return found
        identity = _new_identity(int(clock()))
        _save(directory / FILE_NAME, identity)
        return identity


# --- Helpers ---------------------------------------------------------------------


def _unreadable(path: Path) -> str:
    return (f"{path} isn't an identity this plugin wrote, so Linked Rooms is off on this installation. Moving it "
            "aside makes a new identity, which ends every link this installation has: each person links again")


def _key(value: Any) -> Optional[bytes]:
    if not isinstance(value, str) or not _KEY.match(value):
        return None
    raw = sealing.unb64u(value, IdentityError)
    return raw if len(raw) == KEY_BYTES and sealing.b64u(raw) == value else None


def _identity_from(stored: Any) -> Optional[Identity]:
    if not isinstance(stored, dict) or type(stored.get("version")) is not int or stored["version"] != VERSION:
        return None
    signing_key, sealing_key = _key(stored.get("sign_key")), _key(stored.get("seal_key"))
    created_at = stored.get("created_at")
    if signing_key is None or sealing_key is None or isinstance(created_at, bool) \
            or not isinstance(created_at, int) or created_at < 0:
        return None
    return _identity(signing_key, sealing_key, created_at)


def _identity(signing_key: bytes, sealing_key: bytes, created_at: int) -> Identity:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    signing_public = Ed25519PrivateKey.from_private_bytes(signing_key).public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw)
    sealing_public = X25519PrivateKey.from_private_bytes(sealing_key).public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw)
    return Identity(signing_key, sealing_key, signing_public, sealing_public, created_at)


def _new_identity(created_at: int) -> Identity:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

    raw = (Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    return _identity(Ed25519PrivateKey.generate().private_bytes(*raw),
                     X25519PrivateKey.generate().private_bytes(*raw), created_at)


def _save(path: Path, identity: Identity) -> None:
    encoded = json.dumps({
        "version": VERSION,
        "sign_key": sealing.b64u(identity.signing_key),
        "seal_key": sealing.b64u(identity.sealing_key),
        "created_at": identity.created_at,
    }, separators=(",", ":")) + "\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            # On disk before it's in place: a crash must never leave a file
            # that's refused from then on.
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    path.chmod(0o600)
    # The rename too: after a crash, the identity in place is this one, never
    # none (and so, next time, a new one).
    _sync_directory(path.parent)


def _sync_directory(directory: Path) -> None:
    if os.name == "posix":
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


@contextmanager
def _exclusive(handle: Any) -> Iterator[None]:
    # The same locking as room_choices: flock where there is one, a
    # first-byte lock on Windows.
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    elif msvcrt is not None:
        handle.seek(0)
        for attempt in range(6):  # about a minute
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                break
            except OSError:
                if attempt == 5:
                    raise
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        yield
