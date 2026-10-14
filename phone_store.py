"""Calendar, Reminders, Focus and phone status from Conduit: the store, its
encryption, its queries and the changes Hermes asks the phone to make.

Conduit can share the user's calendars, reminders, which Focus is on and a
snapshot of the phone's state (battery, CarPlay, audio route, motion) with
this profile so Hermes can use them. Each kind is off until the user turns
it on. The phone sends them straight to this host over the dashboard
connection, never through the push relay.

What arrives (designs/phone-v1-contract.md, section 2):

* calendars: the calendars and reminder lists the user picked, replaced
  whole when sent.
* events: one row per occurrence in the shared window (past days to future
  days), as a diff or, with ``full``, the whole window.
* reminders: open ones and those completed in the last 14 days, the same way.
* status and focus: the latest value only, never a history.

The sharing the phone reports in every batch is enforced here again, so a
phone bug can't store more than the user chose: no notes, attendees or URLs
without "details", no attendee emails without that switch, no events at all
when the calendar is write-only or off, and status or Focus forgotten when
either is turned off.

Hermes can't touch EventKit, so writes are pending changes the phone applies
(section 4): the agent's tool adds one here, the phone picks the queue up
with a sealed exchange, applies or asks the user, and reports each outcome
in its next exchange.

Every body is sealed end to end, on top of whatever TLS the dashboard has.
Each device agrees its own key with this profile (``/phone/key``, X25519),
then HKDF-SHA256 and ChaCha20-Poly1305 as in health_store.py, with this
feature's own labels: one key for ingest and one per direction for the
exchange. Wiping this feature never touches health or location.

State lives in the profile's ``conduit-phone.sqlite3`` (owner-only). The
dashboard routes (dashboard process) write it and the ``conduit_calendar`` /
``conduit_phone`` tools (agent process) read it and add changes: the
dashboard loads this file by path, so it imports nothing from the plugin
package.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

FILE_NAME = "conduit-phone.sqlite3"
SCHEMA_VERSION = 1

SALT = b"conduit-phone-v1"
INFO_INGEST = b"conduit-phone-v1 ingest device-to-host"
INFO_EXCHANGE_REQUEST = b"conduit-phone-v1 exchange device-to-host"
INFO_EXCHANGE_ANSWER = b"conduit-phone-v1 exchange host-to-device"
AAD_TAG = "conduit-phone/1"
KID_TAG = b"conduit-phone-kid\n"
NONCE_BYTES = 12
KEY_BYTES = 32

# Exchange operations this host answers: "changes" at /phone/exchange,
# "voice" (a live voice call's read) at /voice-tools/call (contract section 5).
EXCHANGE_OPS = ("changes", "voice")

CALENDAR_SHARING = ("full", "write_only", "off")
CALENDAR_KINDS = ("event", "reminder")
AVAILABILITY = ("busy", "free", "tentative", "unavailable", "none")
EVENT_STATUS = ("confirmed", "tentative", "canceled", "none")
PARTICIPATION = ("accepted", "declined", "tentative", "pending", "none")
ATTENDEE_ROLES = ("required", "optional", "chair", "non_participant", "unknown")
AUDIO_ROUTES = ("speaker", "receiver", "headphones", "bluetooth", "car", "airplay", "other")
NETWORKS = ("wifi", "cellular", "none")
ACTIVITIES = ("stationary", "walking", "running", "cycling", "automotive", "unknown")
CONFIDENCE = ("low", "medium", "high")
PROXIMITY = ("arrive", "leave")
SPANS = ("this", "future")

CHANGE_KINDS = ("add_event", "update_event", "delete_event", "add_reminder", "update_reminder",
                "complete_reminder")
OUTCOMES = ("applied", "edited", "dismissed", "failed", "expired")
EVENT_FIELDS = ("title", "start", "end", "all_day", "calendar_id", "location", "notes", "url", "alarms_min")
REMINDER_FIELDS = ("title", "list_id", "due", "due_all_day", "priority", "notes", "url", "location_alarm")

MAX_DEVICES = 4
MAX_ITEMS = 5000  # calendars + event and reminder upserts and deletions in one batch
MAX_CALENDARS = 100
MAX_TITLE_CHARS = 200
MAX_LOCATION_CHARS = 300
MAX_NOTES_CHARS = 2000
MAX_URL_CHARS = 1000
MAX_ATTENDEES = 30
MAX_NAME_CHARS = 120
MAX_EMAIL_CHARS = 254
MAX_ALARMS = 10
MAX_DEVICE_NAME_CHARS = 60
MAX_FOCUS_LABEL_CHARS = 40
MAX_PLACE_NAME_CHARS = 100
MAX_SOURCE_CHARS = 60
# Pending changes.
MAX_PENDING = 50
MAX_CHANGES_PER_ANSWER = 20
MAX_RESULTS = 50
MAX_WRITE_ALARMS = 3
MAX_ALARM_MINUTES = 10080
MAX_REASON_CHARS = 200
MAX_SESSION_TITLE_CHARS = 80
MAX_NOTE_CHARS = 200
CHANGE_TTL_S = 7 * 24 * 3600
# Finished changes stay this long so the agent can see how they went.
FINISHED_KEPT_S = 30 * 24 * 3600
MAX_WINDOW_DAYS = 400
EARLIEST = datetime(2000, 1, 1, tzinfo=timezone.utc)
LATEST = datetime(2100, 1, 1, tzinfo=timezone.utc)

_DEVICE_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}\Z")
_ITEM_ID = re.compile(r"^[A-Za-z0-9:_-]{1,80}\Z")
_BATCH_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}\Z")
_CHANGE_ID = re.compile(r"^ch_[A-Za-z0-9_-]{8,64}\Z")
_KID = re.compile(r"^[0-9a-f]{32}\Z")
_B64URL = re.compile(r"^[A-Za-z0-9_-]*\Z")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}\Z")
_TZ = re.compile(r"^[A-Za-z0-9_+\-/]{1,64}\Z")
_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?\Z")

# Line breaks, other control characters and invisible format characters
# (bidi overrides, zero-width), which could make a title or name read like
# instructions in the agent's tool result or on the phone's change card.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏ -‮⁠-⁯﻿]+")

_thread_lock = threading.Lock()


class PhoneError(ValueError):
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
        raise PhoneError("malformed", "malformed base64url")
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
        raise PhoneError("bad_key", "That public key can't be used")
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


def ingest_aad(kid: str, device_id: str, batch_id: str) -> bytes:
    return "\n".join([AAD_TAG, "ingest", f"kid={kid}", f"dev={device_id}", f"batch={batch_id}"]).encode("utf-8")


def exchange_aad(direction: str, op: str, kid: str, device_id: str, call_id: str) -> bytes:
    """``direction`` is "request" or "answer"."""
    return "\n".join([AAD_TAG, f"exchange-{direction}", f"op={op}", f"kid={kid}", f"dev={device_id}",
                      f"call={call_id}"]).encode("utf-8")


def seal(key: bytes, plaintext: bytes, aad: bytes, nonce: bytes | None = None) -> tuple[str, str]:
    """Seals one body; the host's side of an exchange answer, and the phone's
    side in tests."""
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    nonce = nonce if nonce is not None else secrets.token_bytes(NONCE_BYTES)
    return b64u(nonce), b64u(ChaCha20Poly1305(key).encrypt(nonce, plaintext, aad))


def open_sealed(key: bytes, nonce: Any, ciphertext: Any, aad: bytes) -> bytes:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    raw_nonce = unb64u(nonce)
    if len(raw_nonce) != NONCE_BYTES:
        raise PhoneError("malformed", "nonce must be 12 bytes")
    raw_ciphertext = unb64u(ciphertext)
    if len(raw_ciphertext) < 16:
        raise PhoneError("malformed", "ct is shorter than its tag")
    try:
        return ChaCha20Poly1305(key).decrypt(raw_nonce, raw_ciphertext, aad)
    except InvalidTag:
        raise PhoneError("bad_seal", "The body didn't open with this device's key; set up calendar sharing again",
                         status=409) from None


# --- Parsing helpers -------------------------------------------------------------


def parse_time(value: Any, field: str, *, latest: datetime | None = None) -> datetime:
    """An ISO 8601 time with an offset (the phone's local offset is kept so
    "Thursday at 3" means the user's)."""
    if not isinstance(value, str) or len(value) > 40:
        raise PhoneError("malformed", f"{field} must be an ISO 8601 time")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        raise PhoneError("malformed", f"{field} must be an ISO 8601 time") from None
    if parsed.tzinfo is None:
        raise PhoneError("malformed", f"{field} needs a UTC offset")
    if parsed < EARLIEST or parsed > (latest or LATEST):
        raise PhoneError("malformed", f"{field} is out of range")
    return parsed


def parse_day(value: Any, field: str) -> date:
    if not isinstance(value, str) or not _DATE.match(value):
        raise PhoneError("malformed", f"{field} must be a date, YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise PhoneError("malformed", f"{field} must be a real date") from None
    if not EARLIEST.date() <= parsed < LATEST.date():
        raise PhoneError("malformed", f"{field} is out of range")
    return parsed


def iso_utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean_text(value: Any, field: str, limit: int, *, cap: bool = False) -> str | None:
    """Short text without control characters. ``cap``: longer text is cut to
    ``limit`` (titles, notes and locations the phone sends, which the contract
    caps) instead of refused."""
    if value is None:
        return None
    if not isinstance(value, str) or (len(value) > limit and not cap) or len(value) > 100_000:
        raise PhoneError("malformed", f"{field} must be text of at most {limit} characters")
    return _CONTROL.sub(" ", value).strip()[:limit].strip() or None


def clean_notes(value: Any, field: str, limit: int = MAX_NOTES_CHARS) -> str | None:
    """Notes keep their line breaks; every other control character goes."""
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 100_000:
        raise PhoneError("malformed", f"{field} must be text")
    lines = (_CONTROL.sub(" ", line).rstrip() for line in value.replace("\r\n", "\n").split("\n"))
    return "\n".join(lines).strip()[:limit].strip() or None


def _bool(value: Any, field: str, default: bool | None = None) -> bool | None:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise PhoneError("malformed", f"{field} must be true or false")
    return value


def _enum(value: Any, field: str, allowed: tuple[str, ...]) -> str | None:
    if value is None:
        return None
    if value not in allowed:
        raise PhoneError("malformed", f"{field} must be one of {', '.join(allowed)}")
    return value


def _number(value: Any, field: str, low: float, high: float) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise PhoneError("malformed", f"{field} must be a finite number")
    if not low <= value <= high:
        raise PhoneError("malformed", f"{field} is out of range")
    return float(value)


def _whole(value: Any, field: str, low: int, high: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise PhoneError("malformed", f"{field} must be a whole number from {low} to {high}")
    return value


def _item_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _ITEM_ID.match(value):
        raise PhoneError("malformed", f"{field} is malformed")
    return value


def _list(value: Any, field: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise PhoneError("malformed", f"{field} must be a list")
    return value


def _object(value: Any, field: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise PhoneError("malformed", f"{field} must be an object")
    return value


def _url(value: Any, field: str) -> str | None:
    text = clean_text(value, field, MAX_URL_CHARS)
    if text is not None and " " in text:
        raise PhoneError("malformed", f"{field} must be a URL")
    return text


def _decode(value: Any, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


# --- Store -----------------------------------------------------------------------


class PhoneStore:
    def __init__(self, home: Any, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(home) / FILE_NAME
        self.clock = clock

    # Connections ---------------------------------------------------------------

    def exists(self) -> bool:
        return self.path.exists()

    @contextlib.contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with _thread_lock:
            if not self.path.exists():
                # Owner-only from the first byte; SQLite gives the -wal and
                # -shm files the same mode.
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
            # Deleted since the check above (a wipe).
            yield None
            return
        try:
            # Only opening counts as "unreadable"; an error in the caller's
            # own query goes up as it is.
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
        out. A new device key for a known device drops that device's data and
        its pending changes (a reinstall, or sharing turned off and on: the
        phone sends everything again); the same device key again keeps them
        and only refreshes the host key."""
        if not isinstance(body, dict):
            raise PhoneError("malformed", "Expected a JSON object")
        device_id = body.get("device_id")
        if not isinstance(device_id, str) or not _DEVICE_ID.match(device_id):
            raise PhoneError("malformed", "device_id must be 8-64 letters, digits, - or _")
        name = clean_text(body.get("device_name"), "device_name", MAX_DEVICE_NAME_CHARS) or "iPhone"
        device_public = unb64u(body.get("public_key"))
        if len(device_public) != 32:
            raise PhoneError("malformed", "public_key must be a 32-byte X25519 key")
        if not crypto_available():
            raise PhoneError("no_crypto", "Calendar sharing needs the cryptography package on this host", status=501)
        host_private, host_public = new_host_keypair()
        agree(host_private, device_public)  # refuses a low-order key now, not at the first ingest
        kid = key_id(device_public, host_public)
        now = int(self.clock())
        with self._write() as db:
            known = db.execute("SELECT device_public FROM devices WHERE id=?", (device_id,)).fetchone()
            count = db.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
            if not known and count >= MAX_DEVICES:
                raise PhoneError("too_many_devices",
                                 f"This profile already keeps calendar data from {MAX_DEVICES} devices; delete one "
                                 "from Conduit first", status=409)
            fresh = not known or bytes(known[0]) != device_public
            if known and fresh:
                _drop_device_data(db, device_id)
            db.execute(
                "INSERT INTO devices(id, name, device_public, host_private, host_public, kid, created_at)"
                " VALUES(?,?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET name=excluded.name, device_public=excluded.device_public,"
                " host_private=excluded.host_private, host_public=excluded.host_public, kid=excluded.kid,"
                " created_at=excluded.created_at",
                (device_id, name, device_public, host_private, host_public, kid, now),
            )
            if known and fresh:
                db.execute("UPDATE devices SET last_seen=NULL, last_sent=NULL, sharing=NULL, tz=NULL,"
                           " window_from=NULL, window_to=NULL WHERE id=?", (device_id,))
            generation = _meta(db, "generation")
        return {"kid": kid, "host_public_key": b64u(host_public), "generation": generation}

    def _device_key(self, db: sqlite3.Connection, device_id: str, kid: str, purpose: bytes = INFO_INGEST) -> bytes:
        row = db.execute("SELECT device_public, host_private, host_public, kid FROM devices WHERE id=?",
                         (device_id,)).fetchone()
        if row is None or row[3] != kid:
            raise PhoneError("unknown_key", "This device's calendar key isn't on this profile; set it up again",
                             status=409)
        device_public, host_private, host_public = bytes(row[0]), bytes(row[1]), bytes(row[2])
        return derive_key(agree(host_private, device_public), device_id, device_public, host_public, purpose)

    # Ingest --------------------------------------------------------------------

    def ingest(self, envelope: Any) -> dict[str, Any]:
        if not isinstance(envelope, dict) or type(envelope.get("v")) is not int or envelope.get("v") != 1:
            raise PhoneError("malformed", "Expected a version 1 sealed batch")
        device_id, kid, batch_id = envelope.get("device_id"), envelope.get("kid"), envelope.get("batch_id")
        if not isinstance(device_id, str) or not _DEVICE_ID.match(device_id):
            raise PhoneError("malformed", "device_id is malformed")
        if not isinstance(kid, str) or not _KID.match(kid):
            raise PhoneError("malformed", "kid is malformed")
        if not isinstance(batch_id, str) or not _BATCH_ID.match(batch_id):
            raise PhoneError("malformed", "batch_id is malformed")
        if not crypto_available():
            raise PhoneError("no_crypto", "Calendar sharing needs the cryptography package on this host", status=501)
        now = int(self.clock())
        with self._write() as db:
            key = self._device_key(db, device_id, kid)
            plaintext = open_sealed(key, envelope.get("nonce"), envelope.get("ct"),
                                    ingest_aad(kid, device_id, batch_id))
            try:
                batch = json.loads(plaintext)
            except (ValueError, RecursionError):
                raise PhoneError("malformed", "The sealed batch isn't JSON") from None
            if not isinstance(batch, dict) or batch.get("batch_id") != batch_id:
                raise PhoneError("malformed", "The sealed batch doesn't match its envelope")
            result = _apply(db, device_id, batch, now)
            _expire(db, now)
            result["pending"] = db.execute("SELECT COUNT(*) FROM changes WHERE device=? AND state='pending'",
                                           (device_id,)).fetchone()[0]
            generation = _meta(db, "generation")
        return {**result, "generation": generation}

    # Exchange --------------------------------------------------------------------

    def open_exchange(self, envelope: Any, ops: tuple[str, ...] = EXCHANGE_OPS
                      ) -> tuple[str, str, dict[str, Any], Callable[[dict[str, Any]], dict[str, str]]]:
        """A device's sealed exchange request: its device id, the operation,
        the opened request, and a function that seals the answer for that
        device alone (``{"call_id", **fields}`` under the host-to-device key).
        An op outside ``ops`` is refused before anything is opened."""
        if not isinstance(envelope, dict) or type(envelope.get("v")) is not int or envelope.get("v") != 1:
            raise PhoneError("malformed", "Expected a version 1 sealed request")
        device_id, kid, call_id, op = (envelope.get(field) for field in ("device_id", "kid", "call_id", "op"))
        if not isinstance(device_id, str) or not _DEVICE_ID.match(device_id):
            raise PhoneError("malformed", "device_id is malformed")
        if not isinstance(kid, str) or not _KID.match(kid):
            raise PhoneError("malformed", "kid is malformed")
        if not isinstance(call_id, str) or not _BATCH_ID.match(call_id):
            raise PhoneError("malformed", "call_id is malformed")
        if op not in EXCHANGE_OPS:
            raise PhoneError("unknown_op", f"op must be one of {', '.join(EXCHANGE_OPS)}")
        if op not in ops:
            raise PhoneError("unknown_op", "Voice reads go to /voice-tools/call" if op == "voice"
                             else f"This route takes op {', '.join(ops)}")
        if not crypto_available():
            raise PhoneError("no_crypto", "Calendar sharing needs the cryptography package on this host", status=501)
        with self._read() as db:
            if db is None:
                raise PhoneError("unknown_key", "This device's calendar key isn't on this profile; set it up again",
                                 status=409)
            request_key = self._device_key(db, device_id, kid, INFO_EXCHANGE_REQUEST)
            answer_key = self._device_key(db, device_id, kid, INFO_EXCHANGE_ANSWER)
        plaintext = open_sealed(request_key, envelope.get("nonce"), envelope.get("ct"),
                                exchange_aad("request", op, kid, device_id, call_id))
        try:
            request = json.loads(plaintext)
        except (ValueError, RecursionError):
            raise PhoneError("malformed", "The sealed request isn't JSON") from None
        if not isinstance(request, dict) or request.get("call_id") != call_id:
            raise PhoneError("malformed", "The sealed request doesn't match its envelope")

        def seal_answer(fields: dict[str, Any]) -> dict[str, str]:
            body = json.dumps({"call_id": call_id, **fields}, ensure_ascii=False, separators=(",", ":"))
            nonce, ct = seal(answer_key, body.encode("utf-8"), exchange_aad("answer", op, kid, device_id, call_id))
            return {"nonce": nonce, "ct": ct}

        return device_id, op, request, seal_answer

    def exchange(self, envelope: Any) -> dict[str, Any]:
        """The ``/phone/exchange`` route: open, run the operation, seal."""
        device_id, _, request, seal_answer = self.open_exchange(envelope, ops=("changes",))
        changes = self.exchange_changes(device_id, request.get("results"))
        return {"v": 1, **seal_answer({"changes": changes})}

    def open_voice_request(self, envelope: Any) -> tuple[str, dict[str, Any], Callable[[dict[str, Any]], dict[str, str]]]:
        """A live voice call's sealed read (``op: "voice"``): the device it
        covers, the opened request ``{call_id, tool, args, provider}``, and the
        sealer for its answer. Only the device can ask, and only about itself.
        A replayed request isn't refused (like health voice reads): it only
        re-runs a read whose answer is sealed to the device, so whoever replays
        it learns nothing, and the route's rate limit caps the load."""
        device_id, _, request, seal_answer = self.open_exchange(envelope, ops=("voice",))
        return device_id, request, seal_answer

    def exchange_changes(self, device_id: str, results: Any) -> list[dict[str, Any]]:
        """Records the phone's outcomes for its changes, then answers every
        change still pending for that device, oldest first (at most
        MAX_CHANGES_PER_ANSWER). Changes older than a week expire first."""
        parsed = _parse_results(results)
        now = int(self.clock())
        with self._write() as db:
            if db.execute("SELECT 1 FROM devices WHERE id=?", (device_id,)).fetchone() is None:
                raise PhoneError("unknown_key", "This device's calendar key isn't on this profile; set it up again",
                                 status=409)
            _expire(db, now)
            for result in parsed:
                # Only this device's own changes, and only while they wait:
                # a repeated result (the phone retrying) changes nothing.
                db.execute("UPDATE changes SET state=?, finished_at=?, item_id=?, note=?, reported_at=NULL"
                           " WHERE id=? AND device=? AND state='pending'",
                           (result["outcome"], now, result.get("item_id"), result.get("note"), result["id"],
                            device_id))
            rows = db.execute(f"SELECT {_CHANGE_COLUMNS} FROM changes WHERE device=? AND state='pending'"
                              " ORDER BY created_at, rowid LIMIT ?", (device_id, MAX_CHANGES_PER_ANSWER)).fetchall()
            if rows:
                db.execute("UPDATE changes SET delivered_at=? WHERE delivered_at IS NULL AND id IN (%s)"
                           % ",".join("?" * len(rows)), (now, *[row[0] for row in rows]))
        return [_change_wire(row) for row in rows]

    # Pending changes (the agent's writes) ----------------------------------------------

    def add_change(self, kind: Any, fields: Any, target: Any = None, reason: Any = None, session_title: Any = None,
                   device: Any = None) -> dict[str, Any]:
        """A change for the phone to apply. Picks the device (the one named,
        else the one that synced last with the sharing the change needs),
        checks the fields against what that device shares, and returns the
        change as the phone will see it, with ``device_name`` and a short
        ``summary`` for the notification."""
        if kind not in CHANGE_KINDS:
            raise PhoneError("invalid", f"kind must be one of {', '.join(CHANGE_KINDS)}")
        fields = {} if fields is None else fields
        if not isinstance(fields, dict):
            raise PhoneError("malformed", "fields must be an object")
        target = {} if target is None else target
        if not isinstance(target, dict):
            raise PhoneError("malformed", "target must be an object")
        reason_text = clean_text(reason, "reason", MAX_REASON_CHARS, cap=True)
        title_text = clean_text(session_title, "session_title", MAX_SESSION_TITLE_CHARS, cap=True)
        need = "events_write" if kind == "add_event" else "events" if kind.endswith("_event") else "reminders"
        now = int(self.clock())
        with self._write() as db:
            _expire(db, now)
            device_id, device_name, sharing = _pick_device(db, device, need)
            if db.execute("SELECT COUNT(*) FROM changes WHERE state='pending'").fetchone()[0] >= MAX_PENDING:
                raise PhoneError("too_many_pending",
                                 f"{MAX_PENDING} changes are already waiting for the phone; wait for the user to "
                                 "answer some, or check them with action pending", status=409)
            clean_target, clean_fields, summary, collection = _check_change(db, device_id, kind, fields, target)
            auto = False
            if kind in ("add_event", "add_reminder") and sharing.get("auto_add") is True:
                calendar = _calendar_row(db, device_id, "event" if kind == "add_event" else "reminder", collection)
                auto = calendar is not None and not calendar["shared"]
            change_id = "ch_" + secrets.token_urlsafe(16)
            db.execute(
                "INSERT INTO changes(id, device, kind, target, fields, auto, reason, session_title, created_at,"
                " expires_at, state) VALUES(?,?,?,?,?,?,?,?,?,?, 'pending')",
                (change_id, device_id, kind, json.dumps(clean_target) if clean_target else None,
                 json.dumps(clean_fields), 1 if auto else 0, reason_text, title_text, now, now + CHANGE_TTL_S))
            row = db.execute(f"SELECT {_CHANGE_COLUMNS} FROM changes WHERE id=?", (change_id,)).fetchone()
            shared = None
            calendar = _calendar_row(db, device_id, "event" if kind.endswith("_event") else "reminder", collection)
            if calendar is not None:
                shared = calendar["shared"]
        return {**_change_wire(row), "device_id": device_id, "device_name": device_name, "summary": summary,
                "calendar": calendar["title"] if calendar else None, "shared_calendar": bool(shared)}

    def changes(self, limit: int = 20, *, mark_reported: bool = True) -> list[dict[str, Any]]:
        """Recent changes, newest first, with their outcomes (the agent's
        ``pending`` action). Outcomes not yet shown to the agent carry
        ``new: true`` once."""
        if not self.path.exists():
            return []
        now = int(self.clock())
        with self._write() as db:
            _expire(db, now)
            rows = db.execute(
                f"SELECT {_CHANGE_COLUMNS}, devices.name FROM changes LEFT JOIN devices ON devices.id=changes.device"
                " ORDER BY changes.created_at DESC, changes.rowid DESC LIMIT ?", (max(1, min(int(limit), 50)),)).fetchall()
            found = [_change_view(row) for row in rows]
            # Only the outcomes shown here count as seen; older ones past the
            # window stay new for the next call.
            shown = [row[0] for row in rows if row[9] != "pending" and row[14] is None]
            if mark_reported and shown:
                db.execute("UPDATE changes SET reported_at=? WHERE reported_at IS NULL AND id IN (%s)"
                           % ",".join("?" * len(shown)), (now, *shown))
        return found

    def unreported_outcomes(self) -> list[dict[str, Any]]:
        """Outcomes the agent hasn't seen yet, oldest first, marked seen."""
        if not self.path.exists():
            return []
        now = int(self.clock())
        with self._write() as db:
            _expire(db, now)
            rows = db.execute(
                f"SELECT {_CHANGE_COLUMNS}, devices.name FROM changes LEFT JOIN devices ON devices.id=changes.device"
                " WHERE state!='pending' AND reported_at IS NULL ORDER BY finished_at LIMIT 20").fetchall()
            if rows:
                db.execute("UPDATE changes SET reported_at=? WHERE id IN (%s)" % ",".join("?" * len(rows)),
                           (now, *[row[0] for row in rows]))
        return [_change_view(row) for row in rows]

    # Status and wipe ---------------------------------------------------------------

    def status(self, device_id: Any = None) -> dict[str, Any]:
        """Counts and dates only, never content."""
        with self._read() as db:
            if db is None:
                return {"stored": False, "generation": None, "device": None}
            answer: dict[str, Any] = {"stored": True, "generation": _meta(db, "generation"), "device": None}
            if isinstance(device_id, str) and _DEVICE_ID.match(device_id):
                row = db.execute("SELECT kid, last_seen, sharing FROM devices WHERE id=?", (device_id,)).fetchone()
                if row is not None:
                    count = lambda table: db.execute(  # noqa: E731
                        f"SELECT COUNT(*) FROM {table} WHERE device=?", (device_id,)).fetchone()[0]
                    pending = db.execute("SELECT COUNT(*) FROM changes WHERE device=? AND state='pending'"
                                         " AND expires_at>?", (device_id, int(self.clock()))).fetchone()[0]
                    answer["device"] = {
                        "kid": row[0], "last_sync": iso_utc(row[1]) if row[1] else None,
                        "sharing": _decode(row[2], None),
                        "counts": {"events": count("events"), "reminders": count("reminders"),
                                   "calendars": count("calendars")},
                        "pending": pending,
                    }
            return answer

    def wipe(self, device_id: Any = None) -> dict[str, Any]:
        """Deletes one device's data, key and changes (pending ones are
        cancelled), or everything when ``device_id`` is None. The file goes
        once no device is left."""
        if device_id is not None and (not isinstance(device_id, str) or not _DEVICE_ID.match(device_id)):
            raise PhoneError("malformed", "device_id is malformed")
        if not self.path.exists():
            return {"deleted": True, "remaining_devices": 0}
        with self._write() as db:
            if device_id is None:
                for table in ("events", "reminders", "calendars", "changes", "devices"):
                    db.execute(f"DELETE FROM {table}")
            else:
                _drop_device_data(db, device_id)
                db.execute("DELETE FROM devices WHERE id=?", (device_id,))
        # Counted again under the writers' lock, so a key registered since
        # the delete stays.
        with _thread_lock:
            remaining = self._device_count()
            if remaining == 0:
                for suffix in ("", "-wal", "-shm", "-journal"):
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(f"{self.path}{suffix}")
        return {"deleted": True, "remaining_devices": remaining}

    def _device_count(self) -> int:
        """Devices in the store; the caller holds ``_thread_lock``."""
        if not self.path.exists():
            return 0
        connection = sqlite3.connect(self.path, timeout=15)
        try:
            return connection.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
        except sqlite3.DatabaseError:
            return 0
        finally:
            connection.close()

    # Queries (the agent's tools) ------------------------------------------------------

    def devices(self) -> list[dict[str, Any]]:
        """Devices that have synced, newest first: name, sharing, time zone,
        window and counts."""
        with self._read() as db:
            if db is None:
                return []
            found = []
            for device_id, name, last_seen, sharing, tz, window_from, window_to, status_at, focus_at in db.execute(
                    "SELECT id, name, last_seen, sharing, tz, window_from, window_to, status_at, focus_at"
                    " FROM devices WHERE last_seen IS NOT NULL ORDER BY last_seen DESC"):
                found.append({
                    "id": device_id, "name": name, "last_seen": last_seen, "sharing": _decode(sharing, {}),
                    "time_zone": tz, "window_from": window_from, "window_to": window_to,
                    "status_at": status_at, "focus_at": focus_at,
                    "events": db.execute("SELECT COUNT(*) FROM events WHERE device=?", (device_id,)).fetchone()[0],
                    "reminders": db.execute("SELECT COUNT(*) FROM reminders WHERE device=? AND completed=0",
                                            (device_id,)).fetchone()[0],
                })
            return found

    @contextlib.contextmanager
    def reader(self, need: str, device: Any = None,
               exact: bool = False) -> Iterator[tuple[sqlite3.Connection, dict[str, Any]] | None]:
        """A read connection and the device to read: the one named, else the
        one that synced last sharing ``need`` ("events", "reminders",
        "status", "focus", "calendars" or "any"). None when nothing is shared.
        Raises PhoneError ``not_shared`` when the named device doesn't share
        it. ``exact``: ``device`` is an id and only that device is read (a
        voice read), never another device by name."""
        with self._read() as db:
            if db is None:
                yield None
                return
            try:
                device_id, name, sharing = _pick_device(db, device, need, exact=exact)
            except PhoneError as error:
                if error.code == "nothing_shared" and not exact:
                    yield None
                    return
                raise
            row = db.execute("SELECT last_seen, tz, window_from, window_to FROM devices WHERE id=?",
                             (device_id,)).fetchone()
            yield db, {"id": device_id, "name": name, "sharing": sharing, "last_seen": row[0], "time_zone": row[1],
                       "window_from": row[2], "window_to": row[3]}

    def calendars(self, db: sqlite3.Connection, device: str, kind: str | None = None) -> list[dict[str, Any]]:
        query = (f"SELECT {_CALENDAR_COLUMNS} FROM calendars WHERE device=?"
                 + (" AND kind=?" if kind else "") + " ORDER BY position")
        return [_calendar_view(row) for row in db.execute(query, (device, kind) if kind else (device,))]

    def events(self, db: sqlite3.Connection, device: str, start: int, end: int, *, query: str | None = None,
               calendar: str | None = None, limit: int = 200) -> tuple[list[dict[str, Any]], bool]:
        sql = (f"SELECT {_EVENT_COLUMNS} FROM events WHERE device=? AND start_utc<? AND (end_utc>? OR start_utc>=?)")
        args: list[Any] = [device, end, start, start]
        if calendar:
            ids = self.resolve_calendar(db, device, "event", calendar)
            sql += " AND calendar_id IN (%s)" % ",".join("?" * len(ids))
            args.extend(ids)
        if query:
            # Filtered before the limit, so a busy range still finds them.
            # SQLite's lower() folds ASCII only; fold in Python so "Événement"
            # finds "événement".
            db.create_function("conduit_fold", 1, lambda text: text.casefold() if isinstance(text, str) else "",
                               deterministic=True)
            sql += (" AND (instr(conduit_fold(title), ?) OR instr(conduit_fold(location), ?)"
                    " OR instr(conduit_fold(notes), ?))")
            needle = query.casefold()
            args.extend([needle, needle, needle])
        sql += " ORDER BY start_utc, end_utc LIMIT ?"
        args.append(limit + 1)
        titles = {row[0]: row[1] for row in db.execute("SELECT id, title FROM calendars WHERE device=?", (device,))}
        found = [_event_view(row, titles) for row in db.execute(sql, args)]
        return found[:limit], len(found) > limit

    def next_events(self, db: sqlite3.Connection, device: str, now: int, count: int) -> list[dict[str, Any]]:
        titles = {row[0]: row[1] for row in db.execute("SELECT id, title FROM calendars WHERE device=?", (device,))}
        return [_event_view(row, titles) for row in db.execute(
            f"SELECT {_EVENT_COLUMNS} FROM events WHERE device=? AND end_utc>? AND coalesce(status,'')!='canceled'"
            " AND coalesce(my_status,'')!='declined' ORDER BY start_utc, end_utc LIMIT ?", (device, now, count))]

    def busy_blocks(self, db: sqlite3.Connection, device: str, start: int, end: int) -> list[tuple[int, int]]:
        """Merged busy time in [start, end): every event that isn't free,
        cancelled or declined. All-day events count only when marked busy or
        unavailable (most are birthdays and holidays)."""
        spans = []
        for event_start, event_end, all_day, availability in db.execute(
                "SELECT start_utc, end_utc, all_day, availability FROM events WHERE device=? AND start_utc<?"
                " AND end_utc>? AND coalesce(status,'')!='canceled' AND coalesce(my_status,'')!='declined'"
                " AND coalesce(availability,'busy')!='free'", (device, end, start)):
            if all_day and availability not in ("busy", "unavailable"):
                continue
            spans.append((max(event_start, start), min(event_end, end)))
        merged: list[tuple[int, int]] = []
        for span_start, span_end in sorted(spans):
            if merged and span_start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], span_end))
            else:
                merged.append((span_start, span_end))
        return merged

    def reminders(self, db: sqlite3.Connection, device: str, *, list_name: str | None = None,
                  status: str = "open", due_by: tuple[str, int | None] | None = None,
                  limit: int = 200) -> tuple[list[dict[str, Any]], bool]:
        """``due_by``: (local date, epoch or None): timed reminders due by the
        epoch (or the end of that date), all-day ones due on or before the
        date."""
        sql = f"SELECT {_REMINDER_COLUMNS} FROM reminders WHERE device=?"
        args: list[Any] = [device]
        if status == "open":
            sql += " AND completed=0"
        elif status == "completed":
            sql += " AND completed=1"
        if list_name:
            ids = self.resolve_calendar(db, device, "reminder", list_name)
            sql += " AND list_id IN (%s)" % ",".join("?" * len(ids))
            args.extend(ids)
        if due_by is not None:
            day, epoch = due_by
            if epoch is None:
                sql += " AND due_date IS NOT NULL AND due_date<=?"
                args.append(day)
            else:
                sql += " AND ((due_utc IS NOT NULL AND due_utc<=?) OR (due_utc IS NULL AND due_date<=?))"
                args.extend([epoch, day])
        sql += (" ORDER BY completed, due_date IS NULL, due_date, coalesce(due_utc, 0),"
                " CASE priority WHEN 0 THEN 10 ELSE priority END, title LIMIT ?")
        args.append(limit + 1)
        titles = {row[0]: row[1] for row in db.execute("SELECT id, title FROM calendars WHERE device=?", (device,))}
        found = [_reminder_view(row, titles) for row in db.execute(sql, args)]
        return found[:limit], len(found) > limit

    def resolve_calendar(self, db: sqlite3.Connection, device: str, kind: str, wanted: str) -> list[str]:
        """Calendar or list ids by id or title (case-insensitive)."""
        rows = db.execute("SELECT id, title FROM calendars WHERE device=? AND kind=?", (device, kind)).fetchall()
        ids = [row[0] for row in rows if row[0] == wanted] or [
            row[0] for row in rows if (row[1] or "").casefold() == wanted.casefold()]
        if not ids:
            word = "calendar" if kind == "event" else "list"
            raise PhoneError(f"unknown_{word}", f"No shared {word} has that name; see overview for the names")
        return ids

    def phone_status(self, db: sqlite3.Connection, device: str) -> dict[str, Any]:
        row = db.execute("SELECT status, status_at, focus, focus_at FROM devices WHERE id=?", (device,)).fetchone()
        return {"status": _decode(row[0], None), "status_at": row[1], "focus": _decode(row[2], None),
                "focus_at": row[3]}


# --- Rows and views -------------------------------------------------------------------

_CALENDAR_COLUMNS = "id, title, kind, color, source, writable, shared, is_default"
_EVENT_COLUMNS = ("id, calendar_id, title, start_local, end_local, all_day, time_zone, location, lat, lon, url,"
                  " availability, status, recurring, organizer_is_me, my_status, alarms, notes, attendees")
_REMINDER_COLUMNS = ("id, list_id, title, due, due_all_day, priority, completed, completed_at, location_alarm,"
                     " recurring, url, notes")
_CHANGE_COLUMNS = ", ".join(f"changes.{column}" for column in (
    "id", "kind", "target", "fields", "auto", "reason", "session_title", "created_at", "expires_at", "state",
    "finished_at", "item_id", "note", "delivered_at", "reported_at", "device"))


def _calendar_view(row: tuple[Any, ...]) -> dict[str, Any]:
    return {"id": row[0], "title": row[1], "kind": row[2], "color": row[3], "source": row[4],
            "writable": bool(row[5]), "shared": bool(row[6]), "default": bool(row[7])}


def _event_view(row: tuple[Any, ...], titles: dict[str, str]) -> dict[str, Any]:
    entry: dict[str, Any] = {"id": row[0], "title": row[2], "start": row[3], "end": row[4]}
    if row[5]:
        entry["all_day"] = True
    if row[1]:
        entry["calendar"] = titles.get(row[1], row[1])
    optional = {"time_zone": row[6], "location": row[7], "url": row[10], "availability": row[11],
                "status": row[12], "my_status": row[15], "notes": row[17]}
    entry.update({key: value for key, value in optional.items() if value not in (None, "", "none")})
    if row[8] is not None and row[9] is not None:
        entry["coordinate"] = {"lat": row[8], "lon": row[9]}
    if row[13]:
        entry["recurring"] = True
    if row[14]:
        entry["organizer_is_me"] = True
    alarms = _decode(row[16], [])
    if alarms:
        entry["alarms_min"] = alarms
    attendees = _decode(row[18], [])
    if attendees:
        entry["attendees"] = attendees
    return entry


def _reminder_view(row: tuple[Any, ...], titles: dict[str, str]) -> dict[str, Any]:
    entry: dict[str, Any] = {"id": row[0], "title": row[2]}
    if row[1]:
        entry["list"] = titles.get(row[1], row[1])
    if row[3]:
        entry["due"] = row[3]
        if row[4]:
            entry["due_all_day"] = True
    if row[5]:
        entry["priority"] = {1: "high", 5: "medium", 9: "low"}.get(row[5], row[5])
    entry["completed"] = bool(row[6])
    if row[7]:
        entry["completed_at"] = row[7]
    alarm = _decode(row[8], None)
    if alarm:
        entry["location_alarm"] = alarm
    if row[9]:
        entry["recurring"] = True
    if row[10]:
        entry["url"] = row[10]
    if row[11]:
        entry["notes"] = row[11]
    return entry


def _change_wire(row: tuple[Any, ...]) -> dict[str, Any]:
    """A change as the phone gets it (contract section 4)."""
    return {"id": row[0], "kind": row[1], "created_at": iso_utc(row[7]), "expires_at": iso_utc(row[8]),
            "auto": bool(row[4]), "reason": row[5], "session_title": row[6], "target": _decode(row[2], None),
            "fields": _decode(row[3], {})}


def _change_view(row: tuple[Any, ...]) -> dict[str, Any]:
    """A change as the agent sees it: what it asked and how it went."""
    fields = _decode(row[3], {})
    target = _decode(row[2], None)
    entry: dict[str, Any] = {"id": row[0], "kind": row[1], "created_at": iso_utc(row[7]),
                             "state": {"pending": "waiting"}.get(row[9], row[9])}
    if fields.get("title"):
        entry["title"] = fields["title"]
    if target:
        entry["target"] = target
    if row[4]:
        entry["auto"] = True
    if row[9] == "pending":
        entry["expires_at"] = iso_utc(row[8])
        entry["seen_by_phone"] = row[13] is not None
    else:
        entry["finished_at"] = iso_utc(row[10]) if row[10] else None
        if row[11]:
            entry["item_id"] = row[11]
        if row[12]:
            entry["note"] = row[12]
        if row[14] is None:
            entry["new"] = True
    if len(row) > 16 and row[16]:
        entry["device"] = row[16]
    return entry


# --- Schema, devices and batch apply ---------------------------------------------------


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
        raise PhoneError("schema", "This calendar store was written by a newer plugin; update the plugin", status=409)
    # One statement at a time: executescript would commit the open transaction.
    for statement in """
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS devices(
            id TEXT PRIMARY KEY, name TEXT NOT NULL,
            device_public BLOB NOT NULL, host_private BLOB NOT NULL, host_public BLOB NOT NULL, kid TEXT NOT NULL,
            created_at INTEGER NOT NULL, last_seen INTEGER, last_sent INTEGER, tz TEXT, sharing TEXT,
            window_from TEXT, window_to TEXT, status TEXT, status_at INTEGER, focus TEXT, focus_at INTEGER);
        CREATE TABLE IF NOT EXISTS calendars(
            device TEXT NOT NULL, id TEXT NOT NULL, title TEXT, kind TEXT NOT NULL, color TEXT, source TEXT,
            writable INTEGER NOT NULL, shared INTEGER NOT NULL, is_default INTEGER NOT NULL, position INTEGER NOT NULL,
            PRIMARY KEY(device, id));
        CREATE TABLE IF NOT EXISTS events(
            device TEXT NOT NULL, id TEXT NOT NULL, calendar_id TEXT, title TEXT,
            start_utc INTEGER NOT NULL, end_utc INTEGER NOT NULL, start_local TEXT NOT NULL, end_local TEXT NOT NULL,
            all_day INTEGER NOT NULL, time_zone TEXT, location TEXT, lat REAL, lon REAL, url TEXT,
            availability TEXT, status TEXT, recurring INTEGER NOT NULL, organizer_is_me INTEGER NOT NULL,
            my_status TEXT, alarms TEXT, notes TEXT, attendees TEXT, received_at INTEGER NOT NULL,
            PRIMARY KEY(device, id));
        CREATE INDEX IF NOT EXISTS events_by_time ON events(device, start_utc);
        CREATE TABLE IF NOT EXISTS reminders(
            device TEXT NOT NULL, id TEXT NOT NULL, list_id TEXT, title TEXT, due TEXT, due_date TEXT, due_utc INTEGER,
            due_all_day INTEGER NOT NULL, priority INTEGER NOT NULL, completed INTEGER NOT NULL, completed_at TEXT,
            location_alarm TEXT, recurring INTEGER NOT NULL, url TEXT, notes TEXT, received_at INTEGER NOT NULL,
            PRIMARY KEY(device, id));
        CREATE TABLE IF NOT EXISTS changes(
            id TEXT PRIMARY KEY, device TEXT NOT NULL, kind TEXT NOT NULL, target TEXT, fields TEXT NOT NULL,
            auto INTEGER NOT NULL, reason TEXT, session_title TEXT, created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL, state TEXT NOT NULL, finished_at INTEGER, item_id TEXT, note TEXT,
            delivered_at INTEGER, reported_at INTEGER);
        CREATE INDEX IF NOT EXISTS changes_by_device ON changes(device, state, created_at);
    """.split(";"):
        if statement.strip():
            db.execute(statement)
    db.execute("INSERT OR REPLACE INTO meta VALUES('schema', ?)", (str(SCHEMA_VERSION),))
    db.execute("INSERT OR IGNORE INTO meta VALUES('generation', ?)", (secrets.token_hex(8),))
    db.execute("INSERT OR IGNORE INTO meta VALUES('created_at', ?)", (str(int(now)),))


def _drop_device_data(db: sqlite3.Connection, device_id: str) -> None:
    for table in ("events", "reminders", "calendars", "changes"):
        db.execute(f"DELETE FROM {table} WHERE device=?", (device_id,))
    db.execute("UPDATE devices SET status=NULL, status_at=NULL, focus=NULL, focus_at=NULL WHERE id=?", (device_id,))


def _expire(db: sqlite3.Connection, now: int) -> None:
    db.execute("UPDATE changes SET state='expired', finished_at=expires_at, reported_at=NULL"
               " WHERE state='pending' AND expires_at<=?", (now,))
    db.execute("DELETE FROM changes WHERE state!='pending' AND coalesce(finished_at, created_at)<?",
               (now - FINISHED_KEPT_S,))


def shares(sharing: dict[str, Any], need: str) -> bool:
    calendar = sharing.get("calendar")
    if need == "events":
        return calendar == "full"
    if need == "events_write":
        return calendar in ("full", "write_only")
    if need == "reminders":
        return sharing.get("reminders") is True
    if need == "calendars":
        return calendar in ("full", "write_only") or sharing.get("reminders") is True
    if need in ("status", "focus"):
        return sharing.get(need) is True
    if need == "phone":
        return sharing.get("status") is True or sharing.get("focus") is True
    return True


_NEED_WORDS = {"events": "their calendar", "events_write": "adding to their calendar", "reminders": "reminders",
               "calendars": "calendars or reminders", "status": "phone status", "focus": "Focus",
               "phone": "phone status or Focus", "any": "anything"}


def _pick_device(db: sqlite3.Connection, wanted: Any, need: str,
                 exact: bool = False) -> tuple[str, str, dict[str, Any]]:
    rows = [(row[0], row[1], _decode(row[2], {})) for row in db.execute(
        "SELECT id, name, sharing FROM devices WHERE last_seen IS NOT NULL ORDER BY last_seen DESC")]
    if exact:
        # Another device's name must never stand in for this one.
        named = [row for row in rows if row[0] == wanted]
        if not named:
            raise PhoneError("nothing_shared", "This iPhone hasn't shared anything yet")
        if need == "events" and named[0][2].get("calendar") == "write_only":
            raise PhoneError("write_only", "The user lets Hermes add events but not read their calendar")
        if not shares(named[0][2], need):
            raise PhoneError("not_shared", f"{named[0][1]} doesn't share {_NEED_WORDS.get(need, need)} with Hermes")
        return named[0]
    if isinstance(wanted, str) and wanted.strip():
        wanted = wanted.strip()
        named = [row for row in rows if row[0] == wanted] or [
            row for row in rows if row[1].casefold() == wanted.casefold()]
        if len(named) != 1:
            raise PhoneError("unknown_device", "No single device has that id or name; use one from overview")
        if not shares(named[0][2], need):
            raise PhoneError("not_shared", f"{named[0][1]} doesn't share {_NEED_WORDS.get(need, need)} with Hermes")
        return named[0]
    for row in rows:
        if shares(row[2], need):
            return row
    if rows and need == "events" and any(row[2].get("calendar") == "write_only" for row in rows):
        raise PhoneError("write_only", "The user lets Hermes add events but not read their calendar")
    raise PhoneError("nothing_shared", f"No device shares {_NEED_WORDS.get(need, need)} with Hermes")


def _calendar_row(db: sqlite3.Connection, device_id: str, kind: str, wanted: str | None) -> dict[str, Any] | None:
    """The named calendar or list, else the device's default of that kind."""
    if wanted:
        row = db.execute(f"SELECT {_CALENDAR_COLUMNS} FROM calendars WHERE device=? AND kind=? AND id=?",
                         (device_id, kind, wanted)).fetchone()
    else:
        row = db.execute(f"SELECT {_CALENDAR_COLUMNS} FROM calendars WHERE device=? AND kind=? AND is_default=1"
                         " ORDER BY position LIMIT 1", (device_id, kind)).fetchone()
    return _calendar_view(row) if row else None


def _parse_sharing(value: Any) -> dict[str, Any]:
    """What the user shares. Anything missing reads as off: a phone that
    leaves a switch out never widens what's kept."""
    if not isinstance(value, dict):
        raise PhoneError("malformed", "sharing must be an object")
    calendar = value.get("calendar", "off")
    if calendar not in CALENDAR_SHARING:
        raise PhoneError("malformed", f"sharing.calendar must be one of {', '.join(CALENDAR_SHARING)}")
    sharing: dict[str, Any] = {"calendar": calendar}
    for field in ("reminders", "details", "attendee_emails", "status", "focus", "auto_add"):
        sharing[field] = bool(_bool(value.get(field), f"sharing.{field}", False))
    # Emails are part of the details.
    sharing["attendee_emails"] = sharing["attendee_emails"] and sharing["details"]
    sharing["past_days"] = _whole(value.get("past_days"), "sharing.past_days", 0, MAX_WINDOW_DAYS)
    sharing["future_days"] = _whole(value.get("future_days"), "sharing.future_days", 0, MAX_WINDOW_DAYS)
    return sharing


def _apply(db: sqlite3.Connection, device_id: str, batch: dict[str, Any], now: int) -> dict[str, Any]:
    sent_at = parse_time(batch.get("sent_at"), "sent_at",
                         latest=datetime.fromtimestamp(now, timezone.utc) + timedelta(days=1))
    sharing = _parse_sharing(batch.get("sharing"))
    tz = batch.get("time_zone")
    if tz is not None and (not isinstance(tz, str) or not _TZ.match(tz)):
        raise PhoneError("malformed", "time_zone is malformed")
    calendars = batch.get("calendars")
    if calendars is not None and not isinstance(calendars, list):
        raise PhoneError("malformed", "calendars must be a list")
    if calendars is not None and len(calendars) > MAX_CALENDARS:
        raise PhoneError("too_large", f"At most {MAX_CALENDARS} calendars", status=413)
    events = _object(batch.get("events"), "events") or {}
    reminders = _object(batch.get("reminders"), "reminders") or {}
    event_upserts, event_deletes = _list(events.get("upsert"), "events.upsert"), _list(events.get("delete"), "events.delete")
    reminder_upserts = _list(reminders.get("upsert"), "reminders.upsert")
    reminder_deletes = _list(reminders.get("delete"), "reminders.delete")
    total = (len(calendars or []) + len(event_upserts) + len(event_deletes) + len(reminder_upserts)
             + len(reminder_deletes))
    if total > MAX_ITEMS:
        raise PhoneError("too_large", f"At most {MAX_ITEMS} items per batch", status=413)

    # Validate everything before writing anything, so a bad item can't leave
    # half a batch behind. What the user doesn't share is dropped unread.
    share_events = sharing["calendar"] == "full"
    share_reminders = sharing["reminders"]
    parsed_calendars = None
    if calendars is not None:
        parsed_calendars = []
        for index, calendar in enumerate(calendars):
            entry = _parse_calendar(calendar, index)
            if entry["kind"] == "event" and (sharing["calendar"] == "off"
                                             or (sharing["calendar"] == "write_only" and not entry["writable"])):
                continue
            if entry["kind"] == "reminder" and not share_reminders:
                continue
            parsed_calendars.append(entry)
    event_full = share_events and _bool(events.get("full"), "events.full", False)
    window = None
    if share_events and (events.get("from") is not None or events.get("to") is not None or event_full):
        start = parse_time(events.get("from"), "events.from")
        end = parse_time(events.get("to"), "events.to")
        if end <= start or end - start > timedelta(days=2 * MAX_WINDOW_DAYS):
            raise PhoneError("malformed", "events.from must be before events.to, within two years")
        window = (start, end)
    parsed_events = [_parse_event(event, index, sharing) for index, event in enumerate(event_upserts)] \
        if share_events else []
    if share_events:
        for index, event_id in enumerate(event_deletes):
            _item_id(event_id, f"events.delete[{index}]")
    reminder_full = share_reminders and _bool(reminders.get("full"), "reminders.full", False)
    parsed_reminders = [_parse_reminder(item, index, sharing) for index, item in enumerate(reminder_upserts)] \
        if share_reminders else []
    if share_reminders:
        for index, reminder_id in enumerate(reminder_deletes):
            _item_id(reminder_id, f"reminders.delete[{index}]")
    status = _parse_status(batch.get("status"), now) if sharing["status"] and batch.get("status") is not None else None
    focus = _parse_focus(batch.get("focus"), now) if sharing["focus"] and batch.get("focus") is not None else None

    stored = db.execute("SELECT last_sent, status_at, focus_at FROM devices WHERE id=?", (device_id,)).fetchone()
    sent_epoch = int(sent_at.timestamp())
    stale = stored[0] is not None and sent_epoch < stored[0]

    # Sharing turned off: forget what was kept, whatever the batch's age.
    if sharing["calendar"] != "full":
        db.execute("DELETE FROM events WHERE device=?", (device_id,))
    if sharing["calendar"] == "off":
        db.execute("DELETE FROM calendars WHERE device=? AND kind='event'", (device_id,))
    elif sharing["calendar"] == "write_only":
        db.execute("DELETE FROM calendars WHERE device=? AND kind='event' AND writable=0", (device_id,))
    if not share_reminders:
        db.execute("DELETE FROM reminders WHERE device=?", (device_id,))
        db.execute("DELETE FROM calendars WHERE device=? AND kind='reminder'", (device_id,))
    if not sharing["details"]:
        for table in ("events", "reminders"):
            db.execute(f"UPDATE {table} SET notes=NULL, url=NULL WHERE device=?", (device_id,))
        db.execute("UPDATE events SET attendees=NULL WHERE device=?", (device_id,))
    elif not sharing["attendee_emails"]:
        for event_id, attendees in db.execute("SELECT id, attendees FROM events WHERE device=? AND attendees"
                                              " LIKE '%\"email\"%'", (device_id,)).fetchall():
            kept = [{key: value for key, value in person.items() if key != "email"}
                    for person in _decode(attendees, [])]
            db.execute("UPDATE events SET attendees=? WHERE device=? AND id=?",
                       (json.dumps(kept) if kept else None, device_id, event_id))
    if not sharing["status"]:
        db.execute("UPDATE devices SET status=NULL, status_at=NULL WHERE id=?", (device_id,))
    if not sharing["focus"]:
        db.execute("UPDATE devices SET focus=NULL, focus_at=NULL WHERE id=?", (device_id,))

    counts = {"events": 0, "reminders": 0, "deleted": 0}
    if stale:
        # An older batch (a retry that arrived late) never undoes a newer
        # one, and never widens what the newer one said is shared; it can
        # only have narrowed it above, which the next batch restores.
        db.execute("UPDATE devices SET last_seen=? WHERE id=?", (now, device_id))
        return {**counts, "stale": True}

    if parsed_calendars is not None:
        db.execute("DELETE FROM calendars WHERE device=?", (device_id,))
        for position, entry in enumerate(parsed_calendars):
            db.execute("INSERT OR REPLACE INTO calendars VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (device_id, entry["id"], entry["title"], entry["kind"], entry["color"], entry["source"],
                        int(entry["writable"]), int(entry["shared"]), int(entry["default"]), position))

    if share_events:
        if event_full:
            counts["deleted"] += db.execute("DELETE FROM events WHERE device=?", (device_id,)).rowcount
        for event_id in event_deletes:
            counts["deleted"] += db.execute("DELETE FROM events WHERE device=? AND id=?",
                                            (device_id, event_id)).rowcount
        for event in parsed_events:
            db.execute("INSERT OR REPLACE INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (device_id, event["id"], event["calendar_id"], event["title"], event["start_utc"],
                        event["end_utc"], event["start_local"], event["end_local"], int(event["all_day"]),
                        event["time_zone"], event["location"], event["lat"], event["lon"], event["url"],
                        event["availability"], event["status"], int(event["recurring"]),
                        int(event["organizer_is_me"]), event["my_status"],
                        json.dumps(event["alarms"]) if event["alarms"] else None, event["notes"],
                        json.dumps(event["attendees"]) if event["attendees"] else None, now))
            counts["events"] += 1
        if window is not None:
            # Only the window is kept.
            start, end = (int(edge.timestamp()) for edge in window)
            counts["deleted"] += db.execute("DELETE FROM events WHERE device=? AND ((end_utc<=? AND start_utc<?) OR start_utc>=?)",
                                            (device_id, start, start, end)).rowcount
            db.execute("UPDATE devices SET window_from=?, window_to=? WHERE id=?",
                       (window[0].isoformat(), window[1].isoformat(), device_id))

    if share_reminders:
        if reminder_full:
            counts["deleted"] += db.execute("DELETE FROM reminders WHERE device=?", (device_id,)).rowcount
        for reminder_id in reminder_deletes:
            counts["deleted"] += db.execute("DELETE FROM reminders WHERE device=? AND id=?",
                                            (device_id, reminder_id)).rowcount
        for item in parsed_reminders:
            db.execute("INSERT OR REPLACE INTO reminders VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (device_id, item["id"], item["list_id"], item["title"], item["due"], item["due_date"],
                        item["due_utc"], int(item["due_all_day"]), item["priority"], int(item["completed"]),
                        item["completed_at"], json.dumps(item["location_alarm"]) if item["location_alarm"] else None,
                        int(item["recurring"]), item["url"], item["notes"], now))
            counts["reminders"] += 1

    # Latest only: an older reading never replaces a newer one.
    if status is not None and (stored[1] is None or status["at"] >= stored[1]):
        db.execute("UPDATE devices SET status=?, status_at=? WHERE id=?",
                   (json.dumps(status["value"]), status["at"], device_id))
    if focus is not None and (stored[2] is None or focus["at"] >= stored[2]):
        db.execute("UPDATE devices SET focus=?, focus_at=? WHERE id=?",
                   (json.dumps(focus["value"]), focus["at"], device_id))
    db.execute("UPDATE devices SET last_seen=?, last_sent=?, sharing=?, tz=COALESCE(?, tz) WHERE id=?",
               (now, sent_epoch, json.dumps(sharing), tz, device_id))
    return counts


def _parse_calendar(value: Any, index: int) -> dict[str, Any]:
    field = f"calendars[{index}]"
    if not isinstance(value, dict):
        raise PhoneError("malformed", f"{field} must be an object")
    color = value.get("color")
    if color is not None and (not isinstance(color, str) or not _COLOR.match(color)):
        raise PhoneError("malformed", f"{field}.color must be #RRGGBB")
    return {
        "id": _item_id(value.get("id"), f"{field}.id"),
        "title": clean_text(value.get("title"), f"{field}.title", MAX_TITLE_CHARS, cap=True),
        "kind": _enum(value.get("kind"), f"{field}.kind", CALENDAR_KINDS) or "event",
        "color": color,
        "source": clean_text(value.get("source"), f"{field}.source", MAX_SOURCE_CHARS, cap=True),
        "writable": bool(_bool(value.get("writable"), f"{field}.writable", False)),
        "shared": bool(_bool(value.get("shared"), f"{field}.shared", False)),
        "default": bool(_bool(value.get("default"), f"{field}.default", False)),
    }


def _parse_event(value: Any, index: int, sharing: dict[str, Any]) -> dict[str, Any]:
    field = f"events.upsert[{index}]"
    if not isinstance(value, dict):
        raise PhoneError("malformed", f"{field} must be an object")
    start = parse_time(value.get("start"), f"{field}.start")
    end = parse_time(value.get("end"), f"{field}.end")
    if end < start or end - start > timedelta(days=MAX_WINDOW_DAYS):
        raise PhoneError("malformed", f"{field} must end after it starts")
    tz = value.get("time_zone")
    if tz is not None and (not isinstance(tz, str) or not _TZ.match(tz)):
        raise PhoneError("malformed", f"{field}.time_zone is malformed")
    calendar_id = value.get("calendar_id")
    alarms = _list(value.get("alarms_min"), f"{field}.alarms_min")
    if len(alarms) > MAX_ALARMS:
        raise PhoneError("malformed", f"{field}.alarms_min holds at most {MAX_ALARMS}")
    lat = _number(value.get("lat"), f"{field}.lat", -90, 90)
    lon = _number(value.get("lon"), f"{field}.lon", -180, 180)
    entry: dict[str, Any] = {
        "id": _item_id(value.get("id"), f"{field}.id"),
        "calendar_id": None if calendar_id is None else _item_id(calendar_id, f"{field}.calendar_id"),
        "title": clean_text(value.get("title"), f"{field}.title", MAX_TITLE_CHARS, cap=True),
        "start_utc": int(start.timestamp()), "end_utc": int(end.timestamp()),
        "start_local": start.isoformat(), "end_local": end.isoformat(),
        "all_day": bool(_bool(value.get("all_day"), f"{field}.all_day", False)),
        "time_zone": tz,
        "location": clean_text(value.get("location"), f"{field}.location", MAX_LOCATION_CHARS, cap=True),
        "lat": lat if lon is not None else None, "lon": lon if lat is not None else None,
        "availability": _enum(value.get("availability"), f"{field}.availability", AVAILABILITY),
        "status": _enum(value.get("status"), f"{field}.status", EVENT_STATUS),
        "recurring": bool(_bool(value.get("recurring"), f"{field}.recurring", False)),
        "organizer_is_me": bool(_bool(value.get("organizer_is_me"), f"{field}.organizer_is_me", False)),
        "my_status": _enum(value.get("my_status"), f"{field}.my_status", PARTICIPATION),
        "alarms": [_whole(minutes, f"{field}.alarms_min", -40320, 40320) for minutes in alarms],
        "url": None, "notes": None, "attendees": None,
    }
    # Checked whatever the switches say, kept only with "details" on.
    url = _url(value.get("url"), f"{field}.url")
    notes = clean_notes(value.get("notes"), f"{field}.notes")
    attendees = _parse_attendees(value.get("attendees"), field, sharing["attendee_emails"])
    if sharing["details"]:
        entry.update(url=url, notes=notes, attendees=attendees or None)
    return entry


def _parse_attendees(value: Any, field: str, emails: bool) -> list[dict[str, Any]]:
    people = []
    for position, person in enumerate(_list(value, f"{field}.attendees")[:MAX_ATTENDEES]):
        where = f"{field}.attendees[{position}]"
        if not isinstance(person, dict):
            raise PhoneError("malformed", f"{where} must be an object")
        entry = {
            "name": clean_text(person.get("name"), f"{where}.name", MAX_NAME_CHARS, cap=True),
            "email": clean_text(person.get("email"), f"{where}.email", MAX_EMAIL_CHARS),
            "status": _enum(person.get("status"), f"{where}.status", PARTICIPATION),
            "role": _enum(person.get("role"), f"{where}.role", ATTENDEE_ROLES),
        }
        if not emails:
            entry["email"] = None
        entry = {key: item for key, item in entry.items() if item is not None}
        if entry.get("name") or entry.get("email"):
            people.append(entry)
    return people


def _parse_due(value: Any, all_day: Any, field: str) -> tuple[str | None, str | None, int | None, bool]:
    """(due as sent, local date, epoch for a timed due or None, all-day)."""
    if value is None:
        return None, None, None, False
    if isinstance(value, str) and _DATE.match(value):
        day = parse_day(value, field)
        return value, day.isoformat(), None, True
    moment = parse_time(value, field)
    return moment.isoformat(), moment.date().isoformat(), int(moment.timestamp()), bool(all_day)


def _parse_reminder(value: Any, index: int, sharing: dict[str, Any]) -> dict[str, Any]:
    field = f"reminders.upsert[{index}]"
    if not isinstance(value, dict):
        raise PhoneError("malformed", f"{field} must be an object")
    list_id = value.get("list_id")
    all_day = _bool(value.get("due_all_day"), f"{field}.due_all_day", False)
    due, due_date, due_utc, all_day = _parse_due(value.get("due"), all_day, f"{field}.due")
    completed_at = value.get("completed_at")
    alarm = _parse_location_alarm(value.get("location_alarm"), f"{field}.location_alarm")
    url = _url(value.get("url"), f"{field}.url")
    notes = clean_notes(value.get("notes"), f"{field}.notes")
    return {
        "id": _item_id(value.get("id"), f"{field}.id"),
        "list_id": None if list_id is None else _item_id(list_id, f"{field}.list_id"),
        "title": clean_text(value.get("title"), f"{field}.title", MAX_TITLE_CHARS, cap=True),
        "due": due, "due_date": due_date, "due_utc": due_utc, "due_all_day": all_day,
        "priority": _whole(value.get("priority"), f"{field}.priority", 0, 9) or 0,
        "completed": bool(_bool(value.get("completed"), f"{field}.completed", False)),
        "completed_at": None if completed_at is None else parse_time(completed_at, f"{field}.completed_at").isoformat(),
        "location_alarm": alarm,
        "recurring": bool(_bool(value.get("recurring"), f"{field}.recurring", False)),
        "url": url if sharing["details"] else None,
        "notes": notes if sharing["details"] else None,
    }


def _parse_location_alarm(value: Any, field: str) -> dict[str, str] | None:
    alarm = _object(value, field)
    if alarm is None:
        return None
    name = clean_text(alarm.get("name"), f"{field}.name", MAX_PLACE_NAME_CHARS, cap=True)
    proximity = _enum(alarm.get("proximity"), f"{field}.proximity", PROXIMITY)
    if not name or not proximity:
        raise PhoneError("malformed", f"{field} needs a name and a proximity (arrive or leave)")
    return {"name": name, "proximity": proximity}


def _parse_status(value: Any, now: int) -> dict[str, Any]:
    status = _object(value, "status") or {}
    at = parse_time(status.get("at"), "status.at", latest=datetime.fromtimestamp(now, timezone.utc) + timedelta(days=1))
    clean = {
        "battery": _number(status.get("battery"), "status.battery", 0, 1),
        "charging": _bool(status.get("charging"), "status.charging"),
        "low_power": _bool(status.get("low_power"), "status.low_power"),
        "carplay": _bool(status.get("carplay"), "status.carplay"),
        "audio_route": _enum(status.get("audio_route"), "status.audio_route", AUDIO_ROUTES),
        "network": _enum(status.get("network"), "status.network", NETWORKS),
        "activity": _enum(status.get("activity"), "status.activity", ACTIVITIES),
        "activity_confidence": _enum(status.get("activity_confidence"), "status.activity_confidence", CONFIDENCE),
    }
    return {"at": int(at.timestamp()), "value": {key: item for key, item in clean.items() if item is not None}}


def _parse_focus(value: Any, now: int) -> dict[str, Any]:
    focus = _object(value, "focus") or {}
    at = parse_time(focus.get("at"), "focus.at", latest=datetime.fromtimestamp(now, timezone.utc) + timedelta(days=1))
    active = _bool(focus.get("active"), "focus.active")
    if active is None:
        raise PhoneError("malformed", "focus.active must be true or false")
    clean = {"active": active,
             "label": clean_text(focus.get("label"), "focus.label", MAX_FOCUS_LABEL_CHARS, cap=True) if active else None,
             "hold_calls": _bool(focus.get("hold_calls"), "focus.hold_calls") if active else None}
    return {"at": int(at.timestamp()), "value": {key: item for key, item in clean.items() if item is not None}}


def _parse_results(value: Any) -> list[dict[str, Any]]:
    results = _list(value, "results")
    if len(results) > MAX_RESULTS:
        raise PhoneError("too_large", f"At most {MAX_RESULTS} results per exchange", status=413)
    parsed = []
    for index, result in enumerate(results):
        field = f"results[{index}]"
        if not isinstance(result, dict):
            raise PhoneError("malformed", f"{field} must be an object")
        change_id = result.get("id")
        if not isinstance(change_id, str) or not _CHANGE_ID.match(change_id):
            raise PhoneError("malformed", f"{field}.id is malformed")
        outcome = _enum(result.get("outcome"), f"{field}.outcome", OUTCOMES)
        if outcome is None:
            raise PhoneError("malformed", f"{field}.outcome is required")
        item_id = result.get("item_id")
        parsed.append({"id": change_id, "outcome": outcome,
                       "item_id": None if item_id is None else _item_id(item_id, f"{field}.item_id"),
                       "note": clean_text(result.get("note"), f"{field}.note", MAX_NOTE_CHARS, cap=True)})
    return parsed


# --- Change checks ---------------------------------------------------------------------


def _write_time(value: Any, field: str, *, all_day: bool) -> str:
    """A change's time: ISO with an offset, or (all-day) a date."""
    if all_day and isinstance(value, str) and _DATE.match(value):
        return parse_day(value, field).isoformat()
    return parse_time(value, field).isoformat()


def _check_fields(kind: str, fields: dict[str, Any], *, partial: bool) -> dict[str, Any]:
    allowed = EVENT_FIELDS if kind.endswith("_event") else REMINDER_FIELDS
    unknown = sorted(set(fields) - set(allowed))
    if unknown:
        names = [_CONTROL.sub(" ", str(name))[:40] for name in unknown[:10]]
        more = f" (and {len(unknown) - 10} more)" if len(unknown) > 10 else ""
        raise PhoneError("malformed", f"Unknown fields for {kind}: {', '.join(names)}{more}. "
                                      f"Known: {', '.join(allowed)}")
    present = {key: value for key, value in fields.items() if key in allowed}
    if partial and not present:
        raise PhoneError("malformed", f"{kind} needs at least one field to change")
    clean: dict[str, Any] = {}
    if "title" in present or not partial:
        title = clean_text(present.get("title"), "title", MAX_TITLE_CHARS)
        if not title:
            raise PhoneError("malformed", "title is required")
        clean["title"] = title
    for key, limit in (("location", MAX_LOCATION_CHARS),):
        if key in present or (not partial and key in allowed):
            clean[key] = clean_text(present.get(key), key, limit)
    if "notes" in present or not partial:
        notes = present.get("notes")
        if notes is not None and (not isinstance(notes, str) or len(notes) > MAX_NOTES_CHARS):
            raise PhoneError("malformed", f"notes must be text of at most {MAX_NOTES_CHARS} characters")
        clean["notes"] = clean_notes(notes, "notes")
    if "url" in present or not partial:
        clean["url"] = _url(present.get("url"), "url")
    if kind.endswith("_event"):
        all_day = _bool(present.get("all_day"), "all_day", None if partial else False)
        if all_day is not None:
            clean["all_day"] = all_day
        if "start" in present or not partial:
            if present.get("start") is None:
                raise PhoneError("malformed", "start is required")
            clean["start"] = _write_time(present["start"], "start", all_day=bool(all_day))
        if "end" in present or not partial:
            if present.get("end") is None:
                if not partial and not all_day:
                    raise PhoneError("malformed", "end is required unless all_day is true")
                clean["end"] = None
            else:
                clean["end"] = _write_time(present["end"], "end", all_day=bool(all_day))
        if clean.get("start") and clean.get("end") and not _DATE.match(clean["start"]) \
                and not _DATE.match(clean["end"]):
            if datetime.fromisoformat(clean["end"]) < datetime.fromisoformat(clean["start"]):
                raise PhoneError("malformed", "end must not be before start")
        if "calendar_id" in present or not partial:
            calendar_id = present.get("calendar_id")
            clean["calendar_id"] = None if calendar_id is None else _item_id(calendar_id, "calendar_id")
        if "alarms_min" in present or not partial:
            alarms = present.get("alarms_min")
            if alarms is None:
                clean["alarms_min"] = None
            else:
                if not isinstance(alarms, list) or len(alarms) > MAX_WRITE_ALARMS:
                    raise PhoneError("malformed", f"alarms_min is a list of up to {MAX_WRITE_ALARMS} minute counts")
                clean["alarms_min"] = [_whole(minutes, "alarms_min", 0, MAX_ALARM_MINUTES) for minutes in alarms]
    else:
        if "list_id" in present or not partial:
            list_id = present.get("list_id")
            clean["list_id"] = None if list_id is None else _item_id(list_id, "list_id")
        if "due" in present or "due_all_day" in present or not partial:
            all_day = _bool(present.get("due_all_day"), "due_all_day", False)
            due = present.get("due")
            if due is None:
                clean["due"], clean["due_all_day"] = None, False
            elif isinstance(due, str) and _DATE.match(due):
                clean["due"], clean["due_all_day"] = parse_day(due, "due").isoformat(), True
            else:
                clean["due"], clean["due_all_day"] = parse_time(due, "due").isoformat(), bool(all_day)
        if "priority" in present or not partial:
            clean["priority"] = _whole(present.get("priority"), "priority", 0, 9) or 0
        if "location_alarm" in present or not partial:
            clean["location_alarm"] = _parse_location_alarm(present.get("location_alarm"), "location_alarm")
    return clean


def _check_change(db: sqlite3.Connection, device_id: str, kind: str, fields: dict[str, Any],
                  target: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any], str, str | None]:
    """(target, fields, summary, calendar or list id) for a change, or
    PhoneError."""
    is_event = kind.endswith("_event")
    collection_kind = "event" if is_event else "reminder"
    existing: dict[str, Any] | None = None
    clean_target: dict[str, Any] | None = None
    if kind in ("update_event", "delete_event"):
        unknown = sorted(set(target) - {"event_id", "span"})
        if unknown:
            raise PhoneError("malformed", "target is {event_id, span}")
        event_id = _item_id(target.get("event_id"), "event_id")
        span = _enum(target.get("span"), "span", SPANS) or "this"
        row = db.execute("SELECT title, start_local, end_local, all_day, calendar_id, recurring FROM events"
                         " WHERE device=? AND id=?", (device_id, event_id)).fetchone()
        if row is None:
            raise PhoneError("unknown_event", "No shared event has that id; find it with action events first")
        existing = {"title": row[0], "start": row[1], "end": row[2], "all_day": bool(row[3]), "calendar_id": row[4]}
        if span == "future" and not row[5]:
            span = "this"
        clean_target = {"event_id": event_id, "span": span}
    elif kind in ("update_reminder", "complete_reminder"):
        unknown = sorted(set(target) - {"reminder_id"})
        if unknown:
            raise PhoneError("malformed", "target is {reminder_id}")
        reminder_id = _item_id(target.get("reminder_id"), "reminder_id")
        row = db.execute("SELECT title, due, list_id FROM reminders WHERE device=? AND id=?",
                         (device_id, reminder_id)).fetchone()
        if row is None:
            raise PhoneError("unknown_reminder", "No shared reminder has that id; find it with action reminders first")
        existing = {"title": row[0], "due": row[1], "list_id": row[2]}
        clean_target = {"reminder_id": reminder_id}
    elif target:
        raise PhoneError("malformed", f"{kind} takes no target")

    if kind == "delete_event":
        if fields:
            raise PhoneError("malformed", "delete_event takes no fields")
        clean_fields: dict[str, Any] = {}
    elif kind == "complete_reminder":
        unknown = sorted(set(fields) - {"completed"})
        if unknown:
            raise PhoneError("malformed", "complete_reminder takes only completed")
        clean_fields = {"completed": _bool(fields.get("completed"), "completed", True)}
    else:
        clean_fields = _check_fields(kind, fields, partial=kind.startswith("update_"))

    collection = clean_fields.get("calendar_id" if is_event else "list_id")
    if collection is None and existing is not None:
        collection = existing.get("calendar_id" if is_event else "list_id")
    if collection is not None:
        calendar = _calendar_row(db, device_id, collection_kind, collection)
        word = "calendar" if is_event else "list"
        if calendar is None and (clean_fields.get("calendar_id" if is_event else "list_id") is not None):
            raise PhoneError(f"unknown_{word}", f"No shared {word} has that id; see overview")
        if calendar is not None and not calendar["writable"]:
            raise PhoneError("read_only", f"The {word} {calendar['title'] or collection!r} is read-only on the phone")
    merged = {**(existing or {}), **{key: value for key, value in clean_fields.items() if value is not None}}
    return clean_target, clean_fields, _summary(kind, merged), collection


def _summary(kind: str, item: dict[str, Any]) -> str:
    """One line for the notification: "Dentist · Thu 16 Oct 15:00"."""
    title = item.get("title") or ("Event" if kind.endswith("_event") else "Reminder")
    when = item.get("start") if kind.endswith("_event") else item.get("due")
    return f"{title} · {format_when(when)}" if when else title


def format_when(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    try:
        if _DATE.match(value):
            return date.fromisoformat(value).strftime("%a %d %b")
        moment = datetime.fromisoformat(value)
    except ValueError:
        return ""
    if moment.hour == 0 and moment.minute == 0:
        return moment.strftime("%a %d %b")
    return moment.strftime("%a %d %b %H:%M")
