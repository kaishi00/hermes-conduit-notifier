"""What Shortcuts automations hand to Conduit: the inbox store and its encryption.

iOS lets the user run a shortcut when something happens on the phone: a
notification arrives from an app they pick (iOS 27), a message or an email
comes in, they tap to pay with Wallet, scan an NFC tag, the phone hears a
smoke alarm or a doorbell, CarPlay connects. Conduit's "Log to Hermes"
action takes what the automation hands it and posts it here as one event,
quietly: no chat turn and no push. Hermes reads the events with the
``conduit_inbox`` tool when it needs them ("what did my bank send today?"),
and anything that reads Hermes's state (Pulse, a scheduled job) can too.

Nothing arrives unless the user built an automation that sends it. The phone
drops anything that looks like a sign-in code before it leaves, and this
store drops it again, so a phone bug can't keep one.

Events are other people's words (a text from a friend, an email, an app's
notification), so the tool hands them to the model as material to read,
never as instructions.

Every ingest body is sealed end to end as in health_store: each device
agrees its own key with this profile (``/inbox/key``, X25519), HKDF-SHA256
and ChaCha20-Poly1305, with the inbox's own labels. Events are kept for the
days the phone asks (KEEP_DAYS by default, at most MAX_KEEP_DAYS) and at
most MAX_EVENTS_PER_DEVICE per device, oldest out first.

State lives in the profile's ``conduit-inbox.sqlite3`` (owner-only). The
dashboard routes (dashboard process) write it and the ``conduit_inbox`` tool
(agent process) reads it. The dashboard loads this file and inbox_tool.py as
a private package, so it imports nothing else from the plugin.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]
try:
    import msvcrt
except ImportError:  # everything but Windows
    msvcrt = None  # type: ignore[assignment]

FILE_NAME = "conduit-inbox.sqlite3"
SCHEMA_VERSION = 1

SALT = b"conduit-inbox-v1"
INFO_INGEST = b"conduit-inbox-v1 ingest device-to-host"
INFO_ANSWER = b"conduit-inbox-v1 answer host-to-device"
AAD_TAG = "conduit-inbox/1"
KID_TAG = b"conduit-inbox-kid\n"
NONCE_BYTES = 12
KEY_BYTES = 32

# What an automation can hand over. The phone maps each Shortcuts trigger to
# one of these; anything else (CarPlay, an alarm, a Focus) is "other".
KINDS = ("notification", "message", "email", "transaction", "nfc", "sound", "other")

# The maintainer's window: 30 days to start, then deleted (review of #350).
KEEP_DAYS = 30
MAX_KEEP_DAYS = 30
MAX_DEVICES = 4
MAX_EVENTS_PER_BATCH = 50
MAX_EVENTS_PER_DEVICE = 5000
MAX_SOURCE_CHARS = 120
MAX_TITLE_CHARS = 200
MAX_TEXT_CHARS = 4000
MAX_AMOUNT_CHARS = 40
MAX_TRIGGER_CHARS = 40
MAX_EVENT_ID_CHARS = 80
MAX_DEVICE_NAME_CHARS = 60
# Conduit's Settings page reads a device's own events back a page at a time
# (list) and deletes the ones the user picks (delete).
MAX_LIST = 100
MAX_DELETE = 100
EARLIEST = datetime(2000, 1, 1, tzinfo=timezone.utc)

_DEVICE_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}\Z")
_EVENT_ID = re.compile(rf"^[A-Za-z0-9:_-]{{1,{MAX_EVENT_ID_CHARS}}}\Z")
_BATCH_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}\Z")
_KID = re.compile(r"^[0-9a-f]{32}\Z")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}\Z")
_B64URL = re.compile(r"^[A-Za-z0-9_-]*\Z")
_TZ = re.compile(r"^[A-Za-z0-9_+\-/]{1,64}\Z")

# Line breaks, other control characters and invisible format characters
# (bidi overrides, zero-width), which could make a source or title read like
# instructions in the agent's tool result. Text keeps its line breaks.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2060-\u206f\ufeff]+")
_CONTROL_KEEP_LINES = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2060-\u206f\ufeff]+")

# A sign-in code, one-time password or verification link: never kept. The
# phone checks the same words first (ShortcutEventFilter.swift); keep both
# lists in step.
_CODE_WORDS = re.compile(
    r"\b(verification|verify|security|login|log-in|sign-in|signin|one[- ]time|two[- ]factor|authentication|auth|"
    r"confirmation|access)\s+(code|password|pin|link)\b|\b(otp|2fa|passcode)\b|\b(your|the|this|ton|votre)\s+code\b|"
    r"\bcode\s*(is|:|est)|\bcode de (vérification|sécurité|confirmation)\b|"
    r"\bmot de passe (à usage unique|temporaire)\b",
    re.IGNORECASE)
# A link that signs in or confirms an account, which works like a code.
_CODE_LINK = re.compile(r"\b(sign[- ]?in|log[- ]?in|verification|verify|magic|confirm)\b[^\n]{0,80}https?://",
                        re.IGNORECASE)
_CODE_DIGITS = re.compile(r"(?<![\d.,$€£])\b\d{4,8}\b(?![.,]\d)|\b\d{3}[- ]\d{3}\b|\b[A-Z0-9]{3,4}-[A-Z0-9]{3,4}\b")

_thread_lock = threading.Lock()


@contextlib.contextmanager
def _exclusive(handle: Any) -> Iterator[None]:
    # The same locking as calls_store: flock where there is one, a
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


class InboxError(ValueError):
    """A request this store won't take; ``code`` is a short machine word and
    ``status`` the HTTP status the route answers with."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


# --- Encoding and crypto -----------------------------------------------------


def b64u(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def unb64u(value: Any) -> bytes:
    if not isinstance(value, str) or not _B64URL.match(value) or len(value) % 4 == 1:
        raise InboxError("malformed", "malformed base64url")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def crypto_available() -> bool:
    try:
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey  # noqa: F401
        from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305  # noqa: F401
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF  # noqa: F401
    except Exception:
        return False
    return True


def key_id(device_public: bytes, host_public: bytes) -> str:
    return hashlib.sha256(KID_TAG + device_public + host_public).hexdigest()[:32]


def derive_key(shared: bytes, device_id: str, device_public: bytes, host_public: bytes,
               purpose: bytes = INFO_INGEST) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    info = purpose + b"\n" + device_id.encode("ascii") + b"\n" + device_public + host_public
    return HKDF(algorithm=hashes.SHA256(), length=KEY_BYTES, salt=SALT, info=info).derive(shared)


def agree(host_private: bytes, device_public: bytes) -> bytes:
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey

    try:
        shared = X25519PrivateKey.from_private_bytes(host_private).exchange(
            X25519PublicKey.from_public_bytes(device_public))
    except ValueError:  # OpenSSL refuses an all-zero result itself
        shared = bytes(32)
    if shared == bytes(32):
        raise InboxError("bad_key", "That public key can't be used")
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


def list_aad(kid: str, device_id: str, request_id: str) -> bytes:
    return "\n".join([AAD_TAG, "list", f"kid={kid}", f"dev={device_id}", f"req={request_id}"]).encode("utf-8")


def ingest_aad(kid: str, device_id: str, batch_id: str) -> bytes:
    return "\n".join([AAD_TAG, "ingest", f"kid={kid}", f"dev={device_id}", f"batch={batch_id}"]).encode("utf-8")


def seal(key: bytes, plaintext: bytes, aad: bytes, nonce: bytes | None = None) -> tuple[str, str]:
    """The phone's side, here for tests."""
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    nonce = nonce or secrets.token_bytes(NONCE_BYTES)
    return b64u(nonce), b64u(ChaCha20Poly1305(key).encrypt(nonce, plaintext, aad))


def open_sealed(key: bytes, nonce: str, ciphertext: str, aad: bytes) -> bytes:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    raw_nonce = unb64u(nonce)
    if len(raw_nonce) != NONCE_BYTES:
        raise InboxError("malformed", "nonce must be 12 bytes")
    raw_ciphertext = unb64u(ciphertext)
    if len(raw_ciphertext) < 16:
        raise InboxError("malformed", "ct is shorter than its tag")
    try:
        return ChaCha20Poly1305(key).decrypt(raw_nonce, raw_ciphertext, aad)
    except InvalidTag:
        raise InboxError("bad_seal", "The body didn't open with this device's key; set up the inbox again",
                         status=409) from None


# --- Field helpers -------------------------------------------------------------


def parse_time(value: Any, field: str) -> datetime:
    """An ISO 8601 time with an offset."""
    if not isinstance(value, str) or len(value) > 40:
        raise InboxError("malformed", f"{field} must be an ISO 8601 time")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise InboxError("malformed", f"{field} must be an ISO 8601 time") from None
    if parsed.tzinfo is None:
        raise InboxError("malformed", f"{field} needs a UTC offset")
    if parsed < EARLIEST or parsed > datetime.now(timezone.utc) + timedelta(days=2):
        raise InboxError("malformed", f"{field} is out of range")
    return parsed


def iso_utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(value: Any, field: str, limit: int, *, lines: bool = False) -> str | None:
    """Short text, cleaned. Longer than ``limit`` is cut, not refused: an
    email body or a long notification is still worth keeping the start of."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise InboxError("malformed", f"{field} must be text")
    cleaned = (_CONTROL_KEEP_LINES if lines else _CONTROL).sub(" ", value[: limit * 2]).strip()
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1].rstrip() + "…"
    return cleaned or None


def looks_like_code(*parts: str | None) -> bool:
    """A sign-in code, one-time password or verification link: words that
    name one, beside a short run of digits or a dashed code."""
    text = " ".join(part for part in parts if part)
    return bool(_CODE_WORDS.search(text) and _CODE_DIGITS.search(text)) or bool(_CODE_LINK.search(text))


# --- Store -----------------------------------------------------------------------


class InboxStore:
    def __init__(self, home: Any, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(home) / FILE_NAME
        self.clock = clock

    # Connections ---------------------------------------------------------------

    def exists(self) -> bool:
        return self.path.exists()

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        """The writers' lock, as in health_store: a lock file beside the
        database, then this process's thread lock."""
        lock_path = self.path.with_name(f".{self.path.name}.lock")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        with os.fdopen(os.open(lock_path, flags, 0o600), "a+b") as handle, _exclusive(handle), _thread_lock:
            yield

    @contextlib.contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._locked():
            if not self.path.exists():
                os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600))
            os.chmod(self.path, 0o600)
            connection = sqlite3.connect(self.path, timeout=15, isolation_level=None)
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("BEGIN IMMEDIATE")
                try:
                    _migrate(connection, self.clock())
                    yield connection
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
                connection.execute("COMMIT")
            finally:
                connection.close()

    @contextlib.contextmanager
    def _read(self) -> Iterator[sqlite3.Connection | None]:
        if not self.path.exists():
            yield None
            return
        try:
            connection = sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True, timeout=15)
        except sqlite3.DatabaseError:
            yield None
            return
        try:
            try:
                connection.execute("PRAGMA query_only=ON")
                readable = _meta(connection, "schema") == str(SCHEMA_VERSION)
            except sqlite3.DatabaseError:
                readable = False
            yield connection if readable else None
        finally:
            connection.close()

    # Keys ------------------------------------------------------------------------

    def register_key(self, body: Any) -> dict[str, Any]:
        """A device's X25519 public key in, this profile's for that device
        out. A new device key drops what the earlier key sent."""
        if not isinstance(body, dict):
            raise InboxError("malformed", "Expected a JSON object")
        device_id = body.get("device_id")
        if not isinstance(device_id, str) or not _DEVICE_ID.match(device_id):
            raise InboxError("malformed", "device_id must be 8-64 letters, digits, - or _")
        name = _text(body.get("device_name"), "device_name", MAX_DEVICE_NAME_CHARS) or "iPhone"
        device_public = unb64u(body.get("public_key"))
        if len(device_public) != 32:
            raise InboxError("malformed", "public_key must be a 32-byte X25519 key")
        if not crypto_available():
            raise InboxError("no_crypto", "The inbox needs the cryptography package on this host", status=501)
        host_private, host_public = new_host_keypair()
        agree(host_private, device_public)  # refuses a low-order key now, not at the first ingest
        kid = key_id(device_public, host_public)
        now = int(self.clock())
        with self._write() as db:
            known = db.execute("SELECT device_public FROM devices WHERE id=?", (device_id,)).fetchone()
            count = db.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
            if not known and count >= MAX_DEVICES:
                raise InboxError("too_many_devices",
                                 f"This profile already keeps events from {MAX_DEVICES} devices; delete one from "
                                 "Conduit first", status=409)
            if known and bytes(known[0]) != device_public:
                db.execute("DELETE FROM events WHERE device=?", (device_id,))
            db.execute(
                "INSERT INTO devices(id, name, device_public, host_private, host_public, kid, created_at, last_seen,"
                " keep_days) VALUES(?,?,?,?,?,?,?,NULL,?)"
                " ON CONFLICT(id) DO UPDATE SET name=excluded.name, device_public=excluded.device_public,"
                " host_private=excluded.host_private, host_public=excluded.host_public, kid=excluded.kid,"
                " created_at=devices.created_at,"
                " last_seen=CASE WHEN devices.device_public=excluded.device_public THEN devices.last_seen END",
                (device_id, name, device_public, host_private, host_public, kid, now, KEEP_DAYS),
            )
            generation = _meta(db, "generation")
        return {"kid": kid, "host_public_key": b64u(host_public), "generation": generation}

    def _device_key(self, db: sqlite3.Connection, device_id: str, kid: str, purpose: bytes = INFO_INGEST) -> bytes:
        row = db.execute("SELECT device_public, host_private, host_public, kid FROM devices WHERE id=?",
                         (device_id,)).fetchone()
        if row is None or row[3] != kid:
            raise InboxError("unknown_key", "This device's inbox key isn't on this profile; set it up again",
                             status=409)
        device_public, host_private, host_public = bytes(row[0]), bytes(row[1]), bytes(row[2])
        return derive_key(agree(host_private, device_public), device_id, device_public, host_public, purpose)

    # Ingest --------------------------------------------------------------------

    def ingest(self, envelope: Any) -> dict[str, Any]:
        if not isinstance(envelope, dict) or type(envelope.get("v")) is not int or envelope.get("v") != 1:
            raise InboxError("malformed", "Expected a version 1 sealed batch")
        device_id, kid, batch_id = envelope.get("device_id"), envelope.get("kid"), envelope.get("batch_id")
        if not isinstance(device_id, str) or not _DEVICE_ID.match(device_id):
            raise InboxError("malformed", "device_id is malformed")
        if not isinstance(kid, str) or not _KID.match(kid):
            raise InboxError("malformed", "kid is malformed")
        if not isinstance(batch_id, str) or not _BATCH_ID.match(batch_id):
            raise InboxError("malformed", "batch_id is malformed")
        if not crypto_available():
            raise InboxError("no_crypto", "The inbox needs the cryptography package on this host", status=501)
        now = int(self.clock())
        with self._write() as db:
            key = self._device_key(db, device_id, kid)
            plaintext = open_sealed(key, envelope.get("nonce"), envelope.get("ct"),
                                    ingest_aad(kid, device_id, batch_id))
            try:
                batch = json.loads(plaintext)
            except (ValueError, RecursionError):
                raise InboxError("malformed", "The sealed batch isn't JSON") from None
            if not isinstance(batch, dict) or batch.get("batch_id") != batch_id:
                raise InboxError("malformed", "The sealed batch doesn't match its envelope")
            result = _apply(db, device_id, batch, now)
            generation = _meta(db, "generation")
        return {**result, "generation": generation}

    # Conduit's Settings page --------------------------------------------------------

    def list_sealed(self, body: Any, nonce: bytes | None = None) -> dict[str, Any]:
        """A device's own events, newest first, a page at a time, sealed for
        that device alone (the answer key): the words never cross the
        dashboard connection in the clear. ``before`` is the last page's
        ``next``."""
        device_id, kid, request_id = self._settings_call(body)
        limit = body.get("limit", 50)
        if type(limit) is not int or not 1 <= limit <= MAX_LIST:
            raise InboxError("malformed", f"limit must be 1-{MAX_LIST}")
        before = body.get("before")
        if before is not None and (not isinstance(before, list) or len(before) != 2 or type(before[0]) is not int
                                   or not isinstance(before[1], str) or not _EVENT_ID.match(before[1])):
            raise InboxError("malformed", "before must be the last page's next")
        # A read: Settings pages never wait on, or hold, the writers' lock.
        with self._read() as db:
            if db is None:  # there (_settings_call checked), but not readable yet: no key is lost
                raise InboxError("unavailable", "The inbox can't be read right now; try again", status=503)
            key = self._device_key(db, device_id, kid, INFO_ANSWER)
            clauses, params = ["e.device = ?", "e.at_utc >= ? - d.keep_days * 86400"], [device_id, int(self.clock())]
            if before is not None:
                clauses.append("(e.at_utc < ? OR (e.at_utc = ? AND e.id < ?))")
                params.extend([before[0], before[0], before[1]])
            rows = db.execute(
                "SELECT e.id, e.kind, e.at_utc, e.at_local, e.source, e.title, e.text, e.amount"
                " FROM events e JOIN devices d ON d.id = e.device"
                f" WHERE {' AND '.join(clauses)} ORDER BY e.at_utc DESC, e.id DESC LIMIT ?",
                (*params, limit + 1)).fetchall()
            # Each SELECT here is its own snapshot, so a re-registration (and the
            # new key's events) can land between the key check and the read.
            # Every registration mints a new kid, so checking again means the
            # answer is sealed to ``kid`` only if no newer key's events were read.
            row = db.execute("SELECT kid FROM devices WHERE id=?", (device_id,)).fetchone()
            if row is None or row[0] != kid:
                raise InboxError("unknown_key", "This device's inbox key isn't on this profile; set it up again",
                                 status=409)
        events = []
        for event_id, kind, _, at_local, source, title, text, amount in rows[:limit]:
            item = {"id": event_id, "kind": kind, "at": at_local, "source": source, "title": title, "text": text,
                    "amount": amount}
            events.append({name: value for name, value in item.items() if value is not None})
        last = rows[limit - 1] if len(rows) > limit else None
        answer = {"request_id": request_id, "events": events, "next": [last[2], last[0]] if last else None}
        sealed_nonce, ct = seal(key, json.dumps(answer, separators=(",", ":")).encode("utf-8"),
                                list_aad(kid, device_id, request_id), nonce)
        return {"nonce": sealed_nonce, "ct": ct}

    def delete_events(self, body: Any) -> dict[str, Any]:
        """Deletes the events a device names from its own; the rest stay.
        Ids aren't secret, so the request isn't sealed, but it names the
        device's key like the rest."""
        device_id, kid, _ = self._settings_call(body, needs_request=False)
        ids = body.get("ids")
        if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_DELETE or \
                not all(isinstance(event_id, str) and _EVENT_ID.match(event_id) for event_id in ids):
            raise InboxError("malformed", f"ids must list 1-{MAX_DELETE} event ids")
        with self._write() as db:
            self._device_key(db, device_id, kid)
            deleted = db.execute(f"DELETE FROM events WHERE device=? AND id IN ({','.join('?' * len(ids))})",
                                 (device_id, *ids)).rowcount
        return {"deleted": deleted}

    def _settings_call(self, body: Any, needs_request: bool = True) -> tuple[str, str, str]:
        if not isinstance(body, dict):
            raise InboxError("malformed", "Expected a JSON object")
        device_id, kid, request_id = body.get("device_id"), body.get("kid"), body.get("request_id")
        if not isinstance(device_id, str) or not _DEVICE_ID.match(device_id):
            raise InboxError("malformed", "device_id is malformed")
        if not isinstance(kid, str) or not _KID.match(kid):
            raise InboxError("malformed", "kid is malformed")
        if needs_request and (not isinstance(request_id, str) or not _REQUEST_ID.match(request_id)):
            raise InboxError("malformed", "request_id is malformed")
        if not crypto_available():
            raise InboxError("no_crypto", "The inbox needs the cryptography package on this host", status=501)
        if not self.path.exists():
            raise InboxError("unknown_key", "This device's inbox key isn't on this profile; set it up again",
                             status=409)
        return device_id, kid, request_id or ""

    # Status and wipe ---------------------------------------------------------------

    def status(self, device_id: Any = None) -> dict[str, Any]:
        """Counts and dates only: never an event's words."""
        with self._read() as db:
            if db is None:
                return {"stored": False, "generation": None, "device": None}
            answer: dict[str, Any] = {"stored": True, "generation": _meta(db, "generation"), "device": None}
            if isinstance(device_id, str) and _DEVICE_ID.match(device_id):
                row = db.execute("SELECT kid, last_seen, keep_days FROM devices WHERE id=?",
                                 (device_id,)).fetchone()
                if row is not None:
                    # Only what the tool and Settings can still show: within the days kept.
                    cutoff = int(self.clock()) - int(row[2] or KEEP_DAYS) * 86400
                    counts = {kind: count for kind, count in db.execute(
                        "SELECT kind, COUNT(*) FROM events WHERE device=? AND at_utc>=? GROUP BY kind",
                        (device_id, cutoff))}
                    answer["device"] = {"kid": row[0], "last_seen": iso_utc(row[1]) if row[1] else None,
                                        "keep_days": row[2], "events": counts}
            return answer

    def wipe(self, device_id: Any = None) -> dict[str, Any]:
        """Deletes one device's events and key, or everything when
        ``device_id`` is None. The file goes once no device is left."""
        if device_id is not None and (not isinstance(device_id, str) or not _DEVICE_ID.match(device_id)):
            raise InboxError("malformed", "device_id is malformed")
        if not self.path.exists():
            return {"deleted": True, "remaining_devices": 0}
        with self._write() as db:
            if device_id is None:
                db.execute("DELETE FROM events")
                db.execute("DELETE FROM devices")
            else:
                db.execute("DELETE FROM events WHERE device=?", (device_id,))
                db.execute("DELETE FROM devices WHERE id=?", (device_id,))
        with self._locked():
            remaining = self._device_count()
            if remaining == 0:
                for suffix in ("-wal", "-shm", "-journal", ""):
                    with contextlib.suppress(OSError):
                        os.unlink(f"{self.path}{suffix}")
        return {"deleted": True, "remaining_devices": remaining}

    def _device_count(self) -> int:
        if not self.path.exists():
            return 0
        connection = sqlite3.connect(self.path, timeout=15)
        try:
            return connection.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
        except sqlite3.DatabaseError:
            return 0
        finally:
            connection.close()

    # Queries (the agent's tool) ------------------------------------------------------

    def overview(self) -> dict[str, Any]:
        """Per device: what came in, how much, and how far back it goes.
        Events past their device's days are left out, pruned or not."""
        now = int(self.clock())
        with self._read() as db:
            if db is None:
                return {"devices": []}
            devices = []
            for device_id, name, last_seen, keep_days, tz in db.execute(
                    "SELECT id, name, last_seen, keep_days, tz FROM devices ORDER BY last_seen DESC"):
                if not last_seen:
                    continue
                cutoff = now - int(keep_days or KEEP_DAYS) * 86400
                kinds = {kind: count for kind, count in db.execute(
                    "SELECT kind, COUNT(*) FROM events WHERE device=? AND at_utc>=? GROUP BY kind",
                    (device_id, cutoff))}
                sources = [source for (source,) in db.execute(
                    "SELECT source FROM events WHERE device=? AND at_utc>=? AND source IS NOT NULL"
                    " GROUP BY source ORDER BY COUNT(*) DESC LIMIT 12", (device_id, cutoff))]
                span = db.execute("SELECT MIN(at_utc), MAX(at_utc) FROM events WHERE device=? AND at_utc>=?",
                                  (device_id, cutoff)).fetchone()
                devices.append({
                    "id": device_id, "name": name, "time_zone": tz, "last_seen": iso_utc(last_seen),
                    "keep_days": keep_days, "kinds": kinds, "top_sources": sources,
                    "oldest": iso_utc(span[0]) if span[0] else None, "newest": iso_utc(span[1]) if span[1] else None,
                })
            return {"devices": devices}

    def events(self, *, kinds: list[str] | None = None, since: float | None = None, until: float | None = None,
               query: str | None = None, source: str | None = None, device: str | None = None,
               limit: int = 20) -> tuple[list[dict[str, Any]], bool]:
        """Newest first; ``query`` and ``source`` match words anywhere, case
        folded. The second value says more matched than ``limit``."""
        now = int(self.clock())
        with self._read() as db:
            if db is None:
                return [], False
            # SQLite's LOWER folds only ASCII: fold both sides in Python, so
            # "café" finds "CAFÉ".
            db.create_function("fold", 1, _fold, deterministic=True)
            clauses = ["e.at_utc >= ? - d.keep_days * 86400"]
            params: list[Any] = [now]
            if kinds:
                clauses.append(f"e.kind IN ({','.join('?' * len(kinds))})")
                params.extend(kinds)
            if since is not None:
                clauses.append("e.at_utc >= ?")
                params.append(int(since))
            if until is not None:
                clauses.append("e.at_utc <= ?")
                params.append(int(until))
            if device:
                clauses.append("(d.id = ? OR fold(d.name) = fold(?))")
                params.extend([device, device])
            if source:
                clauses.append("fold(COALESCE(e.source, '')) LIKE ? ESCAPE '\\'")
                params.append(f"%{_like(_fold(source))}%")
            if query:
                for word in _fold(query).split()[:6]:
                    clauses.append("fold(COALESCE(e.source, '') || ' ' || COALESCE(e.title, '') || ' ' ||"
                                   " COALESCE(e.text, '')) LIKE ? ESCAPE '\\'")
                    params.append(f"%{_like(word)}%")
            rows = db.execute(
                "SELECT e.id, e.kind, e.at_utc, e.at_local, e.source, e.title, e.text, e.amount, e.trigger,"
                " d.name FROM events e JOIN devices d ON d.id = e.device"
                f" WHERE {' AND '.join(clauses)} ORDER BY e.at_utc DESC, e.received_at DESC LIMIT ?",
                (*params, limit + 1)).fetchall()
        found = []
        for event_id, kind, _, at_local, source_text, title, text, amount, trigger, device_name in rows[:limit]:
            item = {"id": event_id, "kind": kind, "at": at_local, "source": source_text, "title": title,
                    "text": text, "amount": amount, "trigger": trigger, "device": device_name}
            found.append({key: value for key, value in item.items() if value is not None})
        return found, len(rows) > limit


def _fold(text: Any) -> Any:
    return text.casefold() if isinstance(text, str) else text


def _like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# --- Schema and batch apply -----------------------------------------------------------


def _meta(db: sqlite3.Connection, key: str) -> str | None:
    try:
        row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else None


def _migrate(db: sqlite3.Connection, now: float) -> None:
    version = _meta(db, "schema")
    if version == str(SCHEMA_VERSION):
        return
    if version is not None:
        raise InboxError("schema", "This inbox was written by a newer plugin; update the plugin", status=409)
    for statement in """
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS devices(
            id TEXT PRIMARY KEY, name TEXT NOT NULL,
            device_public BLOB NOT NULL, host_private BLOB NOT NULL, host_public BLOB NOT NULL, kid TEXT NOT NULL,
            created_at INTEGER NOT NULL, last_seen INTEGER, keep_days INTEGER NOT NULL, tz TEXT);
        CREATE TABLE IF NOT EXISTS events(
            device TEXT NOT NULL, id TEXT NOT NULL, kind TEXT NOT NULL,
            at_utc INTEGER NOT NULL, at_local TEXT NOT NULL, received_at INTEGER NOT NULL,
            source TEXT, title TEXT, text TEXT, amount TEXT, trigger TEXT,
            PRIMARY KEY(device, id));
        CREATE INDEX IF NOT EXISTS events_by_time ON events(device, at_utc);
    """.split(";"):
        if statement.strip():
            db.execute(statement)
    db.execute("INSERT OR REPLACE INTO meta VALUES('schema', ?)", (str(SCHEMA_VERSION),))
    db.execute("INSERT OR IGNORE INTO meta VALUES('generation', ?)", (secrets.token_hex(8),))
    db.execute("INSERT OR IGNORE INTO meta VALUES('created_at', ?)", (str(int(now)),))


def _apply(db: sqlite3.Connection, device_id: str, batch: dict[str, Any], now: int) -> dict[str, Any]:
    events = batch.get("events")
    if not isinstance(events, list) or not events:
        raise InboxError("malformed", "events must be a non-empty list")
    if len(events) > MAX_EVENTS_PER_BATCH:
        raise InboxError("too_large", f"At most {MAX_EVENTS_PER_BATCH} events per batch", status=413)
    device = batch.get("device") if isinstance(batch.get("device"), dict) else {}
    name = _text(device.get("name"), "device.name", MAX_DEVICE_NAME_CHARS)
    tz = device.get("time_zone")
    if tz is not None and (not isinstance(tz, str) or not _TZ.match(tz)):
        raise InboxError("malformed", "device.time_zone is malformed")
    keep = batch.get("keep_days", KEEP_DAYS)
    if isinstance(keep, bool) or not isinstance(keep, int) or not 1 <= keep <= MAX_KEEP_DAYS:
        raise InboxError("malformed", f"keep_days must be 1 to {MAX_KEEP_DAYS}")
    stored = duplicates = codes = 0
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            raise InboxError("malformed", f"events[{index}] must be an object")
        event_id = event.get("id")
        if not isinstance(event_id, str) or not _EVENT_ID.match(event_id):
            raise InboxError("malformed", f"events[{index}].id is malformed")
        kind = event.get("kind")
        if kind not in KINDS:
            raise InboxError("malformed", f"events[{index}].kind must be one of {', '.join(KINDS)}")
        at = parse_time(event.get("at"), f"events[{index}].at")
        source = _text(event.get("source"), f"events[{index}].source", MAX_SOURCE_CHARS)
        title = _text(event.get("title"), f"events[{index}].title", MAX_TITLE_CHARS)
        text = _text(event.get("text"), f"events[{index}].text", MAX_TEXT_CHARS, lines=True)
        amount = _text(event.get("amount"), f"events[{index}].amount", MAX_AMOUNT_CHARS)
        trigger = _text(event.get("trigger"), f"events[{index}].trigger", MAX_TRIGGER_CHARS)
        if not any((source, title, text, amount, trigger)):
            raise InboxError("malformed", f"events[{index}] carries nothing")
        # Every field: a code or sign-in link in the source or amount stays
        # out too (Conduit's ShortcutEventFilter reads the same ones).
        if looks_like_code(source, title, text, amount):
            codes += 1
            continue
        cursor = db.execute(
            "INSERT OR IGNORE INTO events(device, id, kind, at_utc, at_local, received_at, source, title, text,"
            " amount, trigger) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (device_id, event_id, kind, int(at.timestamp()), at.isoformat(timespec="seconds"), now, source, title,
             text, amount, trigger))
        if cursor.rowcount:
            stored += 1
        else:
            duplicates += 1
    db.execute("UPDATE devices SET last_seen=?, keep_days=?, name=COALESCE(?, name), tz=COALESCE(?, tz) WHERE id=?",
               (now, keep, name, tz, device_id))
    # Older than the device's days, then the oldest past the cap.
    pruned = db.execute("DELETE FROM events WHERE device=? AND at_utc < ?",
                        (device_id, now - keep * 86400)).rowcount
    pruned += db.execute(
        "DELETE FROM events WHERE device=? AND rowid IN (SELECT rowid FROM events WHERE device=?"
        " ORDER BY at_utc DESC LIMIT -1 OFFSET ?)", (device_id, device_id, MAX_EVENTS_PER_DEVICE)).rowcount
    return {"stored": stored, "duplicates": duplicates, "codes_dropped": codes, "pruned": pruned}
