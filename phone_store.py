"""Calendar, Reminders, Focus, phone status, alarms and contacts from
Conduit: the store, its encryption, its queries and the changes Hermes asks
the phone to make.

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
* alarms (0.30.0): the alarms and timers Conduit set for Hermes that are
  still on the phone, replaced whole when sent.
* contacts (0.30.0): the people the user picked in Conduit, replaced whole
  when sent, deleted when the user stops sharing them.

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
import logging
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

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]
try:
    import msvcrt
except ImportError:  # everything but Windows
    msvcrt = None  # type: ignore[assignment]
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

FILE_NAME = "conduit-phone.sqlite3"
SCHEMA_VERSION = 3  # 2: alarms and contacts (plugin 0.30.0); 3: last_contact, prior_sharing (0.51.9)

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
# Sharing switches that take changes away when on: Hermes reads what's
# shared but can't add, change or delete anything in it.
READ_ONLY_SHARING = ("calendar_read_only", "reminders_read_only")
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

CALENDAR_CHANGE_KINDS = ("add_event", "update_event", "delete_event", "add_reminder", "update_reminder",
                         "complete_reminder", "delete_reminder")
# Kinds whose fields carry the item's title and time for the phone's card.
_DISPLAY_ONLY_KINDS = ("delete_event", "complete_reminder", "delete_reminder")
ALARM_CHANGE_KINDS = ("set_alarm", "set_timer", "cancel_alarm")
CHANGE_KINDS = CALENDAR_CHANGE_KINDS + ALARM_CHANGE_KINDS
OUTCOMES = ("applied", "edited", "dismissed", "failed", "expired")
# Set by the host, never by the phone: the phone was set up again (a new key)
# before it fetched the change, so it will never see it.
LOST_NOTE = ("The phone was set up again before it got this change, so it never saw it. Tell the user, and "
             "offer to ask again.")
LOST_DELIVERED_NOTE = ("The phone was set up again after this change was offered to it, before it answered. Ask "
                       "the user whether it's on their phone before asking again, so it isn't added twice.")
# How long a device that checked in but hasn't uploaded since a new key
# holds its place as the device in use, instead of another one standing in.
SETTING_UP_S = 10 * 60
# When a device was last in touch: its last check-in (status) or upload. The
# device in use comes first; a tie in the same second goes to the older row.
_IN_TOUCH = "MAX(COALESCE(last_contact, 0), COALESCE(last_seen, 0))"
_IN_TOUCH_ORDER = f"{_IN_TOUCH} DESC, rowid"
EVENT_FIELDS = ("title", "start", "end", "all_day", "calendar_id", "location", "notes", "url", "alarms_min")
REMINDER_FIELDS = ("title", "list_id", "due", "due_all_day", "priority", "notes", "url", "location_alarm")
ALARM_FIELDS = ("label", "at", "time", "repeats")
TIMER_FIELDS = ("label", "seconds")
ALARM_KINDS = ("alarm", "timer")
ALARM_STATES = ("scheduled", "countdown", "paused", "alerting")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

MAX_DEVICES = 4
MAX_ITEMS = 5000  # calendars + event and reminder upserts and deletions + alarms + contacts in one batch
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
MAX_ALARMS_LISTED = 50
MAX_LABEL_CHARS = 80
MIN_TIMER_S = 5
MAX_TIMER_S = 24 * 3600
MAX_CONTACTS = 200
MAX_CONTACT_VALUES = 10
MAX_RELATION_CHARS = 40
MAX_PHONE_CHARS = 40
MAX_VALUE_LABEL_CHARS = 30
# Finished changes stay this long so the agent can see how they went.
FINISHED_KEPT_S = 30 * 24 * 3600
MAX_WINDOW_DAYS = 400
# How far ahead of this host's clock a batch's sent_at may be.
MAX_CLOCK_SKEW_S = 5 * 60
EARLIEST = datetime(2000, 1, 1, tzinfo=timezone.utc)
LATEST = datetime(2100, 1, 1, tzinfo=timezone.utc)

_DEVICE_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}\Z")
_ITEM_ID = re.compile(r"^[A-Za-z0-9:_-]{1,80}\Z")
_BATCH_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}\Z")
_CHANGE_ID = re.compile(r"^ch_[A-Za-z0-9_-]{8,64}\Z")
_KID = re.compile(r"^[0-9a-f]{32}\Z")
_B64URL = re.compile(r"^[A-Za-z0-9_-]*\Z")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}\Z")
_CLOCK = re.compile(r"^([01]\d|2[0-3]):[0-5]\d\Z")
# A birthday: YYYY-MM-DD, or --MM-DD when the year isn't known.
_BIRTHDAY = re.compile(r"^(\d{4}|-)-(\d{2})-(\d{2})\Z")
# A dialable number: those characters, at least one digit.
_PHONE_NUMBER = re.compile(r"^(?=.*\d)[0-9+()*#.,;/ \-]{1,%d}\Z" % MAX_PHONE_CHARS)
_TZ = re.compile(r"^[A-Za-z0-9_+\-/]{1,64}\Z")
_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?\Z")

# Line breaks, other control characters and invisible format characters
# (bidi overrides, zero-width), which could make a title or name read like
# instructions in the agent's tool result or on the phone's change card.
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2060-\u206f\ufeff]+")

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


def _write_url(value: Any, field: str) -> str | None:
    """A change's link: Conduit keeps only an http or https one and applies
    the rest of the change without anything else, so it's refused here."""
    text = _url(value, field)
    if text is None:
        return None
    try:
        parts = urlsplit(text)
    except ValueError:
        parts = None
    if parts is None or parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise PhoneError("malformed", f"{field} must be an http or https URL")
    return text


def _stored_list(value: Any) -> list[Any] | None:
    """A stored JSON list, or None when it's missing or corrupt."""
    decoded = _decode(value, None)
    return decoded if isinstance(decoded, list) else None


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
    def _locked(self) -> Iterator[None]:
        """The writers' lock: a lock file beside the database, which every
        process (two dashboards, the agent) and thread takes through its own
        handle, then this process's thread lock. The file lock comes first,
        so waiting on another process never holds up other profiles' stores
        here. The lock file outlives a wipe, so a writer waiting on it never
        races the unlink."""
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

    def _open_read(self) -> tuple[sqlite3.Connection, str | None] | None:
        """A read-only connection and the schema it holds (None when it can't
        be read), or None when there's no file (gone since a wipe)."""
        if not self.path.exists():
            return None
        try:
            connection = sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True, timeout=15)
        except sqlite3.DatabaseError:
            return None
        try:
            connection.execute("PRAGMA query_only=ON")
            version = _meta(connection, "schema")
        except sqlite3.DatabaseError:
            version = None
        return connection, version

    @contextlib.contextmanager
    def _read(self) -> Iterator[sqlite3.Connection | None]:
        opened = self._open_read()
        if opened is None:
            yield None
            return
        connection, version = opened
        try:
            if version is not None and version.isdigit() and int(version) < SCHEMA_VERSION:
                # An older plugin's file: upgrade it first. One that can't be
                # upgraded now (another process holds it, a disk error) still
                # has its data, so it isn't read as nothing.
                connection.close()
                try:
                    with self._write():
                        pass
                except (sqlite3.DatabaseError, OSError) as error:
                    # A schema no upgrade reaches (_migrate's 409) goes up as
                    # it is: waiting won't fix it, updating the plugin will.
                    raise PhoneError("upgrade_pending", "The calendar store is being upgraded; try again in a moment",
                                     status=503) from error
                opened = self._open_read()
                if opened is None:
                    yield None
                    return
                connection, version = opened
            # Only opening counts as "unreadable"; an error in the caller's
            # own query goes up as it is.
            yield connection if version == str(SCHEMA_VERSION) else None
        finally:
            connection.close()

    # Keys ------------------------------------------------------------------------

    def register_key(self, body: Any) -> dict[str, Any]:
        """A device's X25519 public key in, this profile's for that device
        out. A new device key for a known device drops that device's data and
        marks its pending changes lost, so the agent hears it (a reinstall,
        or sharing turned off and on: the phone sends everything again); the
        same device key again keeps them and only refreshes the host key."""
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
                # The phone will never see what was waiting for its old key:
                # say so to the agent instead of dropping it unseen.
                db.execute("UPDATE changes SET state='lost', finished_at=?, reported_at=NULL,"
                           " note=CASE WHEN delivered_at IS NULL THEN ? ELSE ? END WHERE device=? AND state='pending'",
                           (now, LOST_NOTE, LOST_DELIVERED_NOTE, device_id))
                _drop_device_data(db, device_id, keep_changes=True)
            db.execute(
                "INSERT INTO devices(id, name, device_public, host_private, host_public, kid, created_at)"
                " VALUES(?,?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET name=excluded.name, device_public=excluded.device_public,"
                " host_private=excluded.host_private, host_public=excluded.host_public, kid=excluded.kid,"
                " created_at=excluded.created_at",
                (device_id, name, device_public, host_private, host_public, kid, now),
            )
            if known and fresh:
                # What it shared before stays, so while it sets up it holds its
                # place only for what it shared (``_pick_device``).
                db.execute("UPDATE devices SET prior_sharing=COALESCE(sharing, prior_sharing), last_seen=NULL,"
                           " last_sent=NULL, sharing=NULL, tz=NULL, window_from=NULL, window_to=NULL WHERE id=?",
                           (device_id,))
            generation = _meta(db, "generation")
        return {"kid": kid, "host_public_key": b64u(host_public), "generation": generation}

    def _still_keyed(self, device_id: str, kid: str) -> None:
        """Refuses once the device has registered again since ``kid`` was
        checked. A sealed answer's read runs in its own connection after the
        request is opened, so a re-registration (and the new key's upload) can
        land in between; checking again after the read means an answer is
        sealed to ``kid`` only if no newer key's data could have been read.
        Every registration mints a new host key, so a kid never comes back."""
        with self._read() as db:
            row = None if db is None else db.execute("SELECT kid FROM devices WHERE id=?", (device_id,)).fetchone()
        if row is None or row[0] != kid:
            raise PhoneError("unknown_key", "This device's calendar key isn't on this profile; set it up again",
                             status=409)

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
            try:
                result = _apply(db, device_id, batch, now)
            except PhoneError as error:
                if error.code != "clock_skew":
                    raise
                # Refused, but what it turns off still goes (committed below):
                # a phone whose clock runs fast can always take sharing back.
                skewed = error
                try:
                    sharing = _parse_sharing(batch.get("sharing"))
                except PhoneError:
                    sharing = None
                if sharing is not None:
                    stored = db.execute("SELECT sharing FROM devices WHERE id=?", (device_id,)).fetchone()
                    sharing = _narrowed(_decode(stored[0], None) if stored else None, sharing)
                    _forget_unshared(db, device_id, sharing)
                    db.execute("UPDATE devices SET sharing=? WHERE id=?", (json.dumps(sharing), device_id))
                db.execute("UPDATE devices SET last_seen=? WHERE id=?", (now, device_id))
                _expire(db, now)
            else:
                skewed = None
                _expire(db, now)
                result["pending"] = db.execute("SELECT COUNT(*) FROM changes WHERE device=? AND state='pending'",
                                               (device_id,)).fetchone()[0]
                generation = _meta(db, "generation")
        if skewed is not None:
            raise skewed
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
            self._still_keyed(device_id, kid)
            body = json.dumps({"call_id": call_id, **fields}, ensure_ascii=False, separators=(",", ":"))
            nonce, ct = seal(answer_key, body.encode("utf-8"), exchange_aad("answer", op, kid, device_id, call_id))
            return {"nonce": nonce, "ct": ct}

        return device_id, op, request, seal_answer

    def exchange(self, envelope: Any) -> dict[str, Any]:
        """The ``/phone/exchange`` route: open, run the operation, seal."""
        device_id, _, request, seal_answer = self.open_exchange(envelope, ops=("changes",))
        changes = self.exchange_changes(device_id, request.get("results"), kid=envelope["kid"])
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

    def exchange_changes(self, device_id: str, results: Any, *, kid: str) -> list[dict[str, Any]]:
        """Records the phone's outcomes for its changes, then answers every
        change still pending for that device, oldest first (at most
        MAX_CHANGES_PER_ANSWER). Changes older than a week expire first.
        ``kid``: the key the request was opened with, checked again in this
        transaction, so a request that outlived a re-registration records and
        reads nothing."""
        parsed = _parse_results(results)
        now = int(self.clock())
        with self._write() as db:
            row = db.execute("SELECT kid FROM devices WHERE id=?", (device_id,)).fetchone()
            if row is None or row[0] != kid:
                raise PhoneError("unknown_key", "This device's calendar key isn't on this profile; set it up again",
                                 status=409)
            _expire(db, now)
            for result in parsed:
                # Only this device's own changes, and only while they wait:
                # a repeated result (the phone retrying) changes nothing.
                db.execute("UPDATE changes SET state=?, finished_at=?, item_id=?, note=?, reported_at=NULL"
                           " WHERE id=? AND device=? AND state IN ('pending', 'lost')",
                           (result["outcome"], now, result.get("item_id"), result.get("note"), result["id"],
                            device_id))
            _refuse_unshared_changes(db, device_id, now)
            rows = db.execute(f"SELECT {_CHANGE_COLUMNS} FROM changes WHERE device=? AND state='pending'"
                              " ORDER BY created_at, rowid LIMIT ?", (device_id, MAX_CHANGES_PER_ANSWER)).fetchall()
            if rows:
                # Offered, not seen: the sealed answer can still be lost on its
                # way. Only a result from the phone says it has the change.
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
        need = change_need(kind)
        now = int(self.clock())
        with self._write() as db:
            _expire(db, now)
            device_id, device_name, sharing = _pick_device(db, device, need, now=now)
            if db.execute("SELECT COUNT(*) FROM changes WHERE state='pending'").fetchone()[0] >= MAX_PENDING:
                raise PhoneError("too_many_pending",
                                 f"{MAX_PENDING} changes are already waiting for the phone; wait for the user to "
                                 "answer some, or check them with action pending", status=409)
            if kind in ALARM_CHANGE_KINDS:
                clean_target, clean_fields, summary = _check_alarm_change(db, device_id, kind, fields, target, now)
                collection = None
            else:
                clean_target, clean_fields, summary, collection = _check_change(db, device_id, kind, fields, target)
            auto = False
            if kind in ("add_event", "add_reminder") and sharing.get("auto_add") is True:
                calendar = _calendar_row(db, device_id, "event" if kind == "add_event" else "reminder", collection)
                auto = calendar is not None and not calendar["shared"]
            elif kind in ("set_alarm", "set_timer") and sharing.get("auto_add") is True:
                # Only the user's own phone rings: nobody else sees it.
                auto = True
            change_id = "ch_" + secrets.token_urlsafe(16)
            db.execute(
                "INSERT INTO changes(id, device, kind, target, fields, auto, reason, session_title, created_at,"
                " expires_at, state) VALUES(?,?,?,?,?,?,?,?,?,?, 'pending')",
                (change_id, device_id, kind, json.dumps(clean_target) if clean_target else None,
                 json.dumps(clean_fields), 1 if auto else 0, reason_text, title_text, now, now + CHANGE_TTL_S))
            row = db.execute(f"SELECT {_CHANGE_COLUMNS} FROM changes WHERE id=?", (change_id,)).fetchone()
            shared = None
            calendar = None
            if kind in CALENDAR_CHANGE_KINDS:
                calendar = _calendar_row(db, device_id, "event" if kind.endswith("_event") else "reminder",
                                         collection)
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
        """Counts and dates only, never content. A device asking about itself
        is in use: that is noted, so its changes go to it (``_pick_device``)."""
        now = int(self.clock())
        stamp = None
        with self._read() as db:
            if db is None:
                return {"stored": False, "generation": None, "device": None}
            answer: dict[str, Any] = {"stored": True, "generation": _meta(db, "generation"), "device": None}
            if isinstance(device_id, str) and _DEVICE_ID.match(device_id):
                row = db.execute(f"SELECT kid, last_seen, sharing, {_IN_TOUCH} FROM devices WHERE id=?",
                                 (device_id,)).fetchone()
                if row is not None:
                    stamp = row[3]
                    count = lambda table: db.execute(  # noqa: E731
                        f"SELECT COUNT(*) FROM {table} WHERE device=?", (device_id,)).fetchone()[0]
                    pending = db.execute("SELECT COUNT(*) FROM changes WHERE device=? AND state='pending'"
                                         " AND expires_at>?", (device_id, now)).fetchone()[0]
                    alarms = _decode(db.execute("SELECT alarms FROM devices WHERE id=?", (device_id,)).fetchone()[0], [])
                    answer["device"] = {
                        "kid": row[0], "last_sync": iso_utc(row[1]) if row[1] else None,
                        "sharing": _decode(row[2], None),
                        "counts": {"events": count("events"), "reminders": count("reminders"),
                                   "calendars": count("calendars"), "contacts": count("contacts"),
                                   "alarms": len(alarms) if isinstance(alarms, list) else 0},
                        "pending": pending,
                    }
        # A known device only, and at most one write a second per device
        # (Conduit asks on every sync pass); a stamp from this second already
        # puts it first.
        if stamp is not None and stamp < now:
            self._touch(device_id, answer["device"]["kid"], now)
        return answer

    def _touch(self, device_id: str, kid: str, now: int) -> None:
        """Notes that a known device checked in. Never creates the store (a
        wipe may have just removed it); a device gone or registered again
        since the read (a new kid every time) is left alone."""
        try:
            connection = sqlite3.connect(f"{self.path.as_uri()}?mode=rw", uri=True, timeout=15,
                                         isolation_level=None)
        except sqlite3.DatabaseError as error:
            logger.warning("conduit phone: couldn't note a check-in (%s); changes follow the last upload", error)
            return
        try:
            # Only forward: a slower, older check-in never pulls it back.
            connection.execute("UPDATE devices SET last_contact=? WHERE id=? AND kid=?"
                               " AND COALESCE(last_contact, 0)<?", (now, device_id, kid, now))
        except sqlite3.DatabaseError as error:
            # The read just upgraded the schema, so this is a busy or broken
            # file: routing falls back to the last upload, and says so.
            logger.warning("conduit phone: couldn't note a check-in (%s); changes follow the last upload", error)
        finally:
            connection.close()

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
                for table in ("events", "reminders", "calendars", "changes", "contacts", "devices"):
                    db.execute(f"DELETE FROM {table}")
            else:
                _drop_device_data(db, device_id)
                db.execute("DELETE FROM devices WHERE id=?", (device_id,))
        # Counted again under the writers' lock, so a key registered since
        # the delete stays.
        with self._locked():
            remaining = self._device_count()
            if remaining == 0:
                # Companions first: a crash part-way never leaves a WAL that a
                # fresh database could pick up.
                for suffix in ("-wal", "-shm", "-journal", ""):
                    # Windows refuses while a reader has it open; the data
                    # is already deleted, and the next wipe tries again.
                    with contextlib.suppress(OSError):
                        os.unlink(f"{self.path}{suffix}")
        return {"deleted": True, "remaining_devices": remaining}

    def _device_count(self) -> int:
        """Devices in the store; the caller holds ``_locked()``."""
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
                    f" FROM devices WHERE last_seen IS NOT NULL ORDER BY {_IN_TOUCH_ORDER}"):
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
               exact: bool = False, for_change: bool = False
               ) -> Iterator[tuple[sqlite3.Connection, dict[str, Any]] | None]:
        """A read connection and the device to read: the one named, else the
        one that synced last sharing ``need`` (see ``shares``: "events",
        "reminders", "status", "focus", "calendars", "phone", "phone_tool",
        "alarms", "contacts" or "any"). None when nothing is shared.
        Raises PhoneError ``not_shared`` when the named device doesn't share
        it. ``exact``: ``device`` is an id and only that device is read (a
        voice read), never another device by name. ``for_change``: the device
        is the one a change will go to, so a device still setting up after a
        new key isn't replaced by another (PhoneError ``device_not_ready``)."""
        with self._read() as db:
            if db is None:
                yield None
                return
            try:
                device_id, name, sharing = _pick_device(db, device, need, exact=exact,
                                                        now=int(self.clock()) if for_change else None)
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

    def alarms(self, db: sqlite3.Connection, device: str) -> tuple[list[dict[str, Any]], int | None]:
        """The alarms and timers Conduit set that are still on the phone, and
        when the phone said so."""
        row = db.execute("SELECT alarms, alarms_at FROM devices WHERE id=?", (device,)).fetchone()
        listed = _decode(row[0], []) if row else []
        return (listed if isinstance(listed, list) else []), (row[1] if row else None)

    def contacts(self, db: sqlite3.Connection, device: str, *, query: str | None = None,
                 limit: int = MAX_CONTACTS) -> list[dict[str, Any]]:
        """The people the user picked, in their order; ``query`` matches a
        name, nickname, relation or organization (case-insensitive)."""
        found = []
        needle = query.casefold() if query else None
        for row in db.execute("SELECT id, name, nickname, relation, phones, emails, birthday, organization"
                              " FROM contacts WHERE device=? ORDER BY position", (device,)):
            if needle and not any(needle in (text or "").casefold() for text in (row[1], row[2], row[3], row[7])):
                continue
            entry = {"id": row[0], "name": row[1], "nickname": row[2], "relation": row[3],
                     "phones": _stored_list(row[4]), "emails": _stored_list(row[5]), "birthday": row[6],
                     "organization": row[7]}
            found.append({key: value for key, value in entry.items() if value})
            if len(found) >= limit:
                break
        return found


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
    for key in ("label", "at", "time", "repeats", "seconds"):
        if fields.get(key):
            entry[key] = fields[key]
    if target:
        entry["target"] = target
    if row[4]:
        entry["auto"] = True
    if row[9] == "pending":
        entry["expires_at"] = iso_utc(row[8])
        # delivered_at is when an answer carried it, not proof the phone got
        # that answer; the phone's result (the state) is the only receipt.
        entry["offered_to_phone"] = row[13] is not None
        if row[13] is not None:
            entry["offered_at"] = iso_utc(row[13])
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
    if version == "1":
        # 0.30.0: the alarms Conduit set and the people the user picked.
        db.execute("ALTER TABLE devices ADD COLUMN alarms TEXT")
        db.execute("ALTER TABLE devices ADD COLUMN alarms_at INTEGER")
        db.execute(_CONTACTS_TABLE)
        version = "2"
    if version == "2":
        # 0.51.9: when each device last checked in, so a change goes to the
        # device in use rather than the last one that uploaded, and what a
        # device set up again shared before its new key.
        columns = {row[1] for row in db.execute("PRAGMA table_info(devices)")}
        if "last_contact" not in columns:
            db.execute("ALTER TABLE devices ADD COLUMN last_contact INTEGER")
        if "prior_sharing" not in columns:
            db.execute("ALTER TABLE devices ADD COLUMN prior_sharing TEXT")
        db.execute("UPDATE meta SET value=? WHERE key='schema'", (str(SCHEMA_VERSION),))
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
            window_from TEXT, window_to TEXT, status TEXT, status_at INTEGER, focus TEXT, focus_at INTEGER,
            alarms TEXT, alarms_at INTEGER, last_contact INTEGER, prior_sharing TEXT);
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
        -- delivered_at: when a sync answer first carried the change, not proof
        -- the phone got it (only its result is)
        CREATE TABLE IF NOT EXISTS changes(
            id TEXT PRIMARY KEY, device TEXT NOT NULL, kind TEXT NOT NULL, target TEXT, fields TEXT NOT NULL,
            auto INTEGER NOT NULL, reason TEXT, session_title TEXT, created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL, state TEXT NOT NULL, finished_at INTEGER, item_id TEXT, note TEXT,
            delivered_at INTEGER, reported_at INTEGER);
        CREATE INDEX IF NOT EXISTS changes_by_device ON changes(device, state, created_at);
    """.split(";") + [_CONTACTS_TABLE]:
        if statement.strip():
            db.execute(statement)
    db.execute("INSERT OR REPLACE INTO meta VALUES('schema', ?)", (str(SCHEMA_VERSION),))
    db.execute("INSERT OR IGNORE INTO meta VALUES('generation', ?)", (secrets.token_hex(8),))
    db.execute("INSERT OR IGNORE INTO meta VALUES('created_at', ?)", (str(int(now)),))


_CONTACTS_TABLE = """
    CREATE TABLE IF NOT EXISTS contacts(
        device TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL, nickname TEXT, relation TEXT, phones TEXT,
        emails TEXT, birthday TEXT, organization TEXT, position INTEGER NOT NULL, received_at INTEGER NOT NULL,
        PRIMARY KEY(device, id))"""


def _drop_device_data(db: sqlite3.Connection, device_id: str, keep_changes: bool = False) -> None:
    for table in ("events", "reminders", "calendars", "contacts") + (() if keep_changes else ("changes",)):
        db.execute(f"DELETE FROM {table} WHERE device=?", (device_id,))
    db.execute("UPDATE devices SET status=NULL, status_at=NULL, focus=NULL, focus_at=NULL, alarms=NULL,"
               " alarms_at=NULL WHERE id=?", (device_id,))


def _refuse_unshared_changes(db: sqlite3.Connection, device_id: str, now: int) -> None:
    """Changes still waiting that the device's sharing no longer allows (the
    user made the calendar read-only, or stopped sharing it, after Hermes
    asked) fail here instead of reaching the phone."""
    row = db.execute("SELECT sharing FROM devices WHERE id=?", (device_id,)).fetchone()
    sharing = _decode(row[0], None) if row else None
    if not isinstance(sharing, dict):
        return
    for change_id, kind in db.execute("SELECT id, kind FROM changes WHERE device=? AND state='pending'",
                                      (device_id,)).fetchall():
        need = change_need(kind)
        if not shares(sharing, need):
            refusal = _read_only(sharing, need)
            note = (f"{refusal}. Don't ask again: tell the user, so they can make it themselves" if refusal
                    else f"The user no longer shares {_NEED_WORDS.get(need, need)} with Hermes")
            db.execute("UPDATE changes SET state='failed', finished_at=?, note=?, reported_at=NULL WHERE id=?",
                       (now, note, change_id))


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
        return calendar in ("full", "write_only") and sharing.get("calendar_read_only") is not True
    if need == "events_change":
        return calendar == "full" and sharing.get("calendar_read_only") is not True
    if need == "reminders":
        return sharing.get("reminders") is True
    if need == "reminders_write":
        return sharing.get("reminders") is True and sharing.get("reminders_read_only") is not True
    if need == "calendars":
        return calendar in ("full", "write_only") or sharing.get("reminders") is True
    if need in ("status", "focus", "alarms", "contacts"):
        return sharing.get(need) is True
    if need == "phone":
        return sharing.get("status") is True or sharing.get("focus") is True
    if need == "phone_tool":
        # conduit_phone: status, Focus, or the alarms Hermes can set.
        return any(sharing.get(kind) is True for kind in ("status", "focus", "alarms"))
    # An unknown need is a caller's mistake: deny it rather than share.
    return need == "any"


_NEED_WORDS = {"events": "their calendar", "events_write": "adding to their calendar",
               "events_change": "changes to their calendar", "reminders": "reminders",
               "reminders_write": "changes to their reminders",
               "calendars": "calendars or reminders", "status": "phone status", "focus": "Focus",
               "phone": "phone status or Focus", "alarms": "alarms and timers", "contacts": "contacts",
               "phone_tool": "phone status, Focus or alarms", "any": "anything"}


def _read_only(sharing: dict[str, Any], need: str) -> PhoneError | None:
    """The refusal for a change to something the user shares read-only."""
    if need in ("events_write", "events_change") and sharing.get("calendar_read_only") is True \
            and sharing.get("calendar") in ("full", "write_only"):
        return PhoneError("shared_read_only", "The user shares their calendar read-only: Hermes can't add, change or delete "
                          "events")
    if need == "reminders_write" and sharing.get("reminders_read_only") is True and sharing.get("reminders") is True:
        return PhoneError("shared_read_only", "The user shares their reminders read-only: Hermes can't add, change, complete "
                          "or delete reminders")
    return None


def _pick_device(db: sqlite3.Connection, wanted: Any, need: str,
                 exact: bool = False, now: int | None = None) -> tuple[str, str, dict[str, Any]]:
    """The device a read or a change is for. Unnamed, it's the device in use:
    the one that checked in (``status``) or uploaded most recently. ``now``
    (for a change) also refuses to stand another device in for one that is
    still setting up after a new key."""
    rows = [(row[0], row[1], _decode(row[2], {})) for row in db.execute(
        f"SELECT id, name, sharing FROM devices WHERE last_seen IS NOT NULL ORDER BY {_IN_TOUCH_ORDER}")]
    if exact:
        # Another device's name must never stand in for this one.
        named = [row for row in rows if row[0] == wanted]
        if not named:
            raise PhoneError("nothing_shared", "This iPhone hasn't shared anything yet")
        if need in ("events", "events_change") and named[0][2].get("calendar") == "write_only":
            raise PhoneError("write_only", "The user lets Hermes add events but not read their calendar")
        refusal = _read_only(named[0][2], need)
        if refusal is not None:
            raise refusal
        if not shares(named[0][2], need):
            raise PhoneError("not_shared", f"{named[0][1]} doesn't share {_NEED_WORDS.get(need, need)} with Hermes")
        return named[0]
    if isinstance(wanted, str) and wanted.strip():
        wanted = wanted.strip()
        named = [row for row in rows if row[0] == wanted] or [
            row for row in rows if row[1].casefold() == wanted.casefold()]
        if len(named) != 1:
            raise PhoneError("unknown_device", "No single device has that id or name; use one from overview")
        refusal = _read_only(named[0][2], need)
        if refusal is not None:
            raise refusal
        if not shares(named[0][2], need):
            raise PhoneError("not_shared", f"{named[0][1]} doesn't share {_NEED_WORDS.get(need, need)} with Hermes")
        return named[0]
    if now is not None:
        # Bounded by the new key's age, not by check-ins: a device that keeps
        # checking in but never uploads must not block changes for good. A
        # device set up again holds its place only for what it shared before
        # (a new one's sharing isn't known yet, so it holds it for anything).
        # The first device in touch that could take the change decides.
        for name, last_seen, created_at, sharing, prior_sharing, last_contact in db.execute(
                "SELECT name, last_seen, created_at, sharing, prior_sharing, last_contact FROM devices"
                f" ORDER BY {_IN_TOUCH_ORDER}"):
            if last_seen is not None:
                if shares(_decode(sharing, {}), need):
                    break
            elif (last_contact is not None and now - created_at < SETTING_UP_S
                  and (prior_sharing is None or shares(_decode(prior_sharing, {}), need))):
                raise PhoneError("device_not_ready", f"{name} is still setting up sharing with Hermes; ask again "
                                 "in a minute, or name another device", status=409)
    for row in rows:
        if shares(row[2], need):
            return row
    if rows and need in ("events", "events_change") and any(row[2].get("calendar") == "write_only"
                                                           for row in rows):
        raise PhoneError("write_only", "The user lets Hermes add events but not read their calendar")
    for row in rows:
        refusal = _read_only(row[2], need)
        if refusal is not None:
            raise refusal
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
    for field in ("reminders", "details", "attendee_emails", "status", "focus", "auto_add", "alarms", "contacts",
                  *READ_ONLY_SHARING):
        sharing[field] = bool(_bool(value.get(field), f"sharing.{field}", False))
    # Add-only and read-only leaves nothing; read-only reminders need reminders.
    if sharing["calendar"] == "write_only" and sharing["calendar_read_only"]:
        sharing["calendar"] = "off"
    sharing["calendar_read_only"] = sharing["calendar_read_only"] and sharing["calendar"] == "full"
    sharing["reminders_read_only"] = sharing["reminders_read_only"] and sharing["reminders"]
    # Emails are part of the details.
    sharing["attendee_emails"] = sharing["attendee_emails"] and sharing["details"]
    sharing["past_days"] = _whole(value.get("past_days"), "sharing.past_days", 0, MAX_WINDOW_DAYS)
    sharing["future_days"] = _whole(value.get("future_days"), "sharing.future_days", 0, MAX_WINDOW_DAYS)
    return sharing


def _forget_unshared(db: sqlite3.Connection, device_id: str, sharing: dict[str, Any]) -> None:
    """Sharing turned off: forget what was kept."""
    if sharing["calendar"] != "full":
        db.execute("DELETE FROM events WHERE device=?", (device_id,))
    if sharing["calendar"] == "off":
        db.execute("DELETE FROM calendars WHERE device=? AND kind='event'", (device_id,))
    elif sharing["calendar"] == "write_only":
        db.execute("DELETE FROM calendars WHERE device=? AND kind='event' AND writable=0", (device_id,))
    if not sharing["reminders"]:
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
    if not sharing["alarms"]:
        db.execute("UPDATE devices SET alarms=NULL, alarms_at=NULL WHERE id=?", (device_id,))
    if not sharing["contacts"]:
        db.execute("DELETE FROM contacts WHERE device=?", (device_id,))


def _narrowed(stored: Any, sharing: dict[str, Any]) -> dict[str, Any]:
    """What stays shared when a batch can only take sharing away: each
    switch as the narrower of the two. Nothing stored reads as nothing
    shared."""
    if not isinstance(stored, dict):
        stored = {}
    rank = {level: index for index, level in enumerate(reversed(CALENDAR_SHARING))}
    narrowed = dict(sharing)
    if rank.get(stored.get("calendar"), 0) < rank[sharing["calendar"]]:
        narrowed["calendar"] = stored.get("calendar") if stored.get("calendar") in rank else "off"
    for field, value in sharing.items():
        if field in READ_ONLY_SHARING:
            # Read-only is the narrower side: on in either keeps it on. A
            # stored sharing from before these switches had them off (and
            # nothing stored shares nothing anyway: the calendar goes off).
            narrowed[field] = value or stored.get(field) is True
        elif isinstance(value, bool):
            narrowed[field] = value and stored.get(field) is True
        elif field in ("past_days", "future_days"):
            kept = stored.get(field)
            narrowed[field] = min(value, kept) if isinstance(kept, int) and not isinstance(kept, bool) else 0
    return narrowed


def _not_ahead(at: datetime, field: str, now: int) -> datetime:
    if at.timestamp() > now + MAX_CLOCK_SKEW_S:
        raise PhoneError("clock_skew", f"{field} is more than {MAX_CLOCK_SKEW_S // 60} minutes ahead of this host;"
                         " check the iPhone's clock and the host's")
    return at


def _apply(db: sqlite3.Connection, device_id: str, batch: dict[str, Any], now: int) -> dict[str, Any]:
    # A batch from the future would shut out every real one after it (they'd
    # all look older), revocations included: only clock skew is allowed.
    sent_at = _not_ahead(parse_time(batch.get("sent_at"), "sent_at"), "sent_at", now)
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
    alarms, contacts = batch.get("alarms"), batch.get("contacts")
    total = (len(calendars or []) + len(event_upserts) + len(event_deletes) + len(reminder_upserts)
             + len(reminder_deletes) + (len(alarms) if isinstance(alarms, list) else 0)
             + (len(contacts) if isinstance(contacts, list) else 0))
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
    parsed_alarms = _parse_alarms(alarms) if sharing["alarms"] and alarms is not None else None
    parsed_contacts = _parse_contacts(contacts) if sharing["contacts"] and contacts is not None else None

    stored = db.execute("SELECT last_sent, status_at, focus_at, window_from, window_to FROM devices WHERE id=?",
                        (device_id,)).fetchone()
    sent_epoch = sent_at.timestamp()
    # A watermark further ahead than skew allows came from before that
    # limit; it doesn't get to hold newer batches off.
    stale = stored[0] is not None and stored[0] <= now + MAX_CLOCK_SKEW_S and sent_epoch < stored[0]
    if window is None and share_events and event_upserts and stored[3] and stored[4]:
        # A diff without a range stays inside the one the user last shared.
        try:
            window = (datetime.fromisoformat(stored[3]), datetime.fromisoformat(stored[4]))
        except (TypeError, ValueError):
            window = None
        if window is not None and (window[0].tzinfo is None or window[1].tzinfo is None or window[1] <= window[0]):
            window = None

    counts = {"events": 0, "reminders": 0, "deleted": 0}
    if stale:
        # An older batch (a retry that arrived late) never undoes a newer
        # one: it neither widens nor narrows what the newer one said is
        # shared. The phone resends only what changed, so nothing would put
        # back what a stale batch took away.
        db.execute("UPDATE devices SET last_seen=? WHERE id=?", (now, device_id))
        return {**counts, "stale": True}

    _forget_unshared(db, device_id, sharing)

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

    if parsed_calendars is not None:
        # A calendar or list taken out of the selection takes its items along.
        for table, column, kind in (("events", "calendar_id", "event"), ("reminders", "list_id", "reminder")):
            kept = [entry["id"] for entry in parsed_calendars if entry["kind"] == kind]
            sql = f"DELETE FROM {table} WHERE device=? AND {column} IS NOT NULL"
            if kept:
                sql += f" AND {column} NOT IN ({','.join('?' * len(kept))})"
            counts["deleted"] += db.execute(sql, (device_id, *kept)).rowcount

    # The alarms Conduit set and the people the user picked: replaced whole.
    if parsed_alarms is not None:
        db.execute("UPDATE devices SET alarms=?, alarms_at=? WHERE id=?",
                   (json.dumps(parsed_alarms), sent_epoch, device_id))
    if parsed_contacts is not None:
        db.execute("DELETE FROM contacts WHERE device=?", (device_id,))
        for position, person in enumerate(parsed_contacts):
            db.execute("INSERT OR REPLACE INTO contacts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (device_id, person["id"], person["name"], person.get("nickname"), person.get("relation"),
                        json.dumps(person["phones"]) if person.get("phones") else None,
                        json.dumps(person["emails"]) if person.get("emails") else None,
                        person.get("birthday"), person.get("organization"), position, now))
        counts["contacts"] = len(parsed_contacts)

    # Latest only: an older reading never replaces a newer one.
    if status is not None and (stored[1] is None or stored[1] > now + MAX_CLOCK_SKEW_S or status["at"] >= stored[1]):
        db.execute("UPDATE devices SET status=?, status_at=? WHERE id=?",
                   (json.dumps(status["value"]), status["at"], device_id))
    if focus is not None and (stored[2] is None or stored[2] > now + MAX_CLOCK_SKEW_S or focus["at"] >= stored[2]):
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
    at = _not_ahead(parse_time(status.get("at"), "status.at"), "status.at", now)
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
    at = _not_ahead(parse_time(focus.get("at"), "focus.at"), "focus.at", now)
    active = _bool(focus.get("active"), "focus.active")
    if active is None:
        raise PhoneError("malformed", "focus.active must be true or false")
    clean = {"active": active,
             "label": clean_text(focus.get("label"), "focus.label", MAX_FOCUS_LABEL_CHARS, cap=True) if active else None,
             "hold_calls": _bool(focus.get("hold_calls"), "focus.hold_calls") if active else None}
    return {"at": int(at.timestamp()), "value": {key: item for key, item in clean.items() if item is not None}}


def _parse_alarms(value: Any) -> list[dict[str, Any]]:
    """The alarms and timers Conduit set for Hermes that are still on the
    phone (not the user's own: AlarmKit shows an app only its own)."""
    alarms = _list(value, "alarms")
    if len(alarms) > MAX_ALARMS_LISTED:
        raise PhoneError("too_large", f"At most {MAX_ALARMS_LISTED} alarms", status=413)
    parsed = []
    for index, alarm in enumerate(alarms):
        field = f"alarms[{index}]"
        if not isinstance(alarm, dict):
            raise PhoneError("malformed", f"{field} must be an object")
        kind = _enum(alarm.get("kind"), f"{field}.kind", ALARM_KINDS)
        if kind is None:
            raise PhoneError("malformed", f"{field}.kind is required")
        fires_at = alarm.get("fires_at")
        entry = {
            "id": _item_id(alarm.get("id"), f"{field}.id"),
            "kind": kind,
            "label": clean_text(alarm.get("label"), f"{field}.label", MAX_LABEL_CHARS, cap=True),
            "fires_at": None if fires_at is None else parse_time(fires_at, f"{field}.fires_at").isoformat(),
            # A countdown has no weekly schedule.
            "repeats": None if kind == "timer" else _weekdays(alarm.get("repeats"), f"{field}.repeats") or None,
            "state": _enum(alarm.get("state"), f"{field}.state", ALARM_STATES) or "scheduled",
            "remaining_s": _whole(alarm.get("remaining_s"), f"{field}.remaining_s", 0, MAX_TIMER_S),
        }
        parsed.append({key: item for key, item in entry.items() if item is not None})
    return parsed


def _weekdays(value: Any, field: str) -> list[str]:
    days = _list(value, field)
    if any(day not in WEEKDAYS for day in days):
        raise PhoneError("malformed", f"{field} is a list of {', '.join(WEEKDAYS)}")
    return [day for day in WEEKDAYS if day in days]


def _parse_contacts(value: Any) -> list[dict[str, Any]]:
    """The people the user picked in Conduit for Hermes to know."""
    contacts = _list(value, "contacts")
    if len(contacts) > MAX_CONTACTS:
        raise PhoneError("too_large", f"At most {MAX_CONTACTS} contacts", status=413)
    parsed, seen = [], set()
    for index, person in enumerate(contacts):
        field = f"contacts[{index}]"
        if not isinstance(person, dict):
            raise PhoneError("malformed", f"{field} must be an object")
        contact_id = _item_id(person.get("id"), f"{field}.id")
        name = clean_text(person.get("name"), f"{field}.name", MAX_NAME_CHARS, cap=True)
        if not name:
            raise PhoneError("malformed", f"{field}.name is required")
        if contact_id in seen:
            raise PhoneError("malformed", f"{field}.id repeats an earlier contact's")
        seen.add(contact_id)
        entry = {
            "id": contact_id, "name": name,
            "nickname": clean_text(person.get("nickname"), f"{field}.nickname", MAX_NAME_CHARS, cap=True),
            "relation": clean_text(person.get("relation"), f"{field}.relation", MAX_RELATION_CHARS, cap=True),
            "organization": clean_text(person.get("organization"), f"{field}.organization", MAX_NAME_CHARS, cap=True),
            "phones": _contact_values(person.get("phones"), f"{field}.phones", "number"),
            "emails": _contact_values(person.get("emails"), f"{field}.emails", "address"),
            "birthday": _birthday(person.get("birthday"), f"{field}.birthday"),
        }
        parsed.append({key: item for key, item in entry.items() if item})
    return parsed


def _contact_values(value: Any, field: str, key: str) -> list[dict[str, str]]:
    values = []
    items = _list(value, field)
    if len(items) > MAX_CONTACT_VALUES:
        raise PhoneError("too_large", f"{field}: at most {MAX_CONTACT_VALUES}", status=413)
    for position, item in enumerate(items):
        where = f"{field}[{position}]"
        if not isinstance(item, dict):
            raise PhoneError("malformed", f"{where} must be an object")
        raw = item.get(key)
        if key == "number":
            text = raw.strip() if isinstance(raw, str) else None
            if not text or not _PHONE_NUMBER.match(text):
                raise PhoneError("malformed", f"{where}.number must be a phone number")
        else:
            text = clean_text(raw, f"{where}.address", MAX_EMAIL_CHARS)
            if not text or "@" not in text or " " in text:
                raise PhoneError("malformed", f"{where}.address must be an email address")
        entry = {key: text}
        label = clean_text(item.get("label"), f"{where}.label", MAX_VALUE_LABEL_CHARS, cap=True)
        if label:
            entry["label"] = label
        values.append(entry)
    return values


def _birthday(value: Any, field: str) -> str | None:
    if value is None:
        return None
    match = _BIRTHDAY.match(value) if isinstance(value, str) else None
    if match is None:
        raise PhoneError("malformed", f"{field} is YYYY-MM-DD, or --MM-DD without the year")
    year = int(match.group(1)) if match.group(1) != "-" else 2000  # a leap year, so 29 Feb is valid
    try:
        date(year, int(match.group(2)), int(match.group(3)))
    except ValueError:
        raise PhoneError("malformed", f"{field} isn't a real date") from None
    return value


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
        clean["url"] = _write_url(present.get("url"), "url")
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
    elif kind in ("update_reminder", "complete_reminder", "delete_reminder"):
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

    if kind in ("delete_event", "delete_reminder"):
        if fields:
            raise PhoneError("malformed", f"{kind} takes no fields")
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
    calendar = _calendar_row(db, device_id, collection_kind, collection)
    word = "calendar" if is_event else "list"
    if collection is not None and calendar is None:
        raise PhoneError(f"unknown_{word}", f"No shared {word} has that id; see overview")
    if calendar is not None and not calendar["writable"]:
        raise PhoneError("read_only", f"The {word} {calendar['title'] or collection!r} is read-only on the phone")
    merged = {**(existing or {}), **{key: value for key, value in clean_fields.items() if value is not None}}
    if kind in _DISPLAY_ONLY_KINDS and existing is not None:
        # The phone ignores these when applying; its card shows them, so the
        # user sees which item a delete or completion is about.
        keys = ("title", "start", "all_day") if is_event else ("title", "due")
        clean_fields = {**{key: existing[key] for key in keys if existing.get(key) is not None}, **clean_fields}
    return clean_target, clean_fields, _summary(kind, merged), collection


def change_need(kind: str) -> str:
    """The sharing a change kind needs on its device."""
    if kind == "add_event":
        return "events_write"
    if kind.endswith("_event"):
        return "events_change"
    if kind in ALARM_CHANGE_KINDS:
        return "alarms"
    return "reminders_write"


def _check_alarm_change(db: sqlite3.Connection, device_id: str, kind: str, fields: dict[str, Any],
                        target: dict[str, Any], now: int) -> tuple[dict[str, Any] | None, dict[str, Any], str]:
    """(target, fields, summary) for an alarm or timer change, or PhoneError."""
    if kind == "cancel_alarm":
        if fields:
            raise PhoneError("malformed", "cancel_alarm takes no fields")
        if sorted(set(target) - {"alarm_id"}):
            raise PhoneError("malformed", "target is {alarm_id}")
        alarm_id = _item_id(target.get("alarm_id"), "alarm_id")
        listed = _decode(db.execute("SELECT alarms FROM devices WHERE id=?", (device_id,)).fetchone()[0], [])
        listed = listed if isinstance(listed, list) else []
        alarm = next((item for item in listed if isinstance(item, dict) and item.get("id") == alarm_id), None)
        if alarm is None:
            raise PhoneError("unknown_alarm", "No alarm or timer Hermes set has that id; see action alarms")
        return {"alarm_id": alarm_id}, {}, "Cancel " + alarm_summary(alarm)
    if target:
        raise PhoneError("malformed", f"{kind} takes no target")
    allowed = ALARM_FIELDS if kind == "set_alarm" else TIMER_FIELDS
    unknown = sorted(set(fields) - set(allowed))
    if unknown:
        names = [_CONTROL.sub(" ", str(name))[:40] for name in unknown[:10]]
        raise PhoneError("malformed", f"Unknown fields for {kind}: {', '.join(names)}. Known: {', '.join(allowed)}")
    clean: dict[str, Any] = {"label": clean_text(fields.get("label"), "label", MAX_LABEL_CHARS)}
    if kind == "set_timer":
        seconds = _whole(fields.get("seconds"), "seconds", MIN_TIMER_S, MAX_TIMER_S)
        if seconds is None:
            raise PhoneError("malformed", "seconds is required")
        clean["seconds"] = seconds
    else:
        at, clock = fields.get("at"), fields.get("time")
        if (at is None) == (clock is None):
            raise PhoneError("malformed", "Give at (a one-off ISO time with offset) or time (HH:MM, with repeats "
                                          "for a repeating alarm), not both")
        repeats = _weekdays(fields.get("repeats"), "repeats")
        if at is not None:
            if repeats:
                raise PhoneError("malformed", "repeats goes with time, not with at")
            moment = parse_time(at, "at")
            if moment.timestamp() <= now:
                raise PhoneError("in_past", "at is in the past")
            clean["at"] = moment.isoformat()
        else:
            if not isinstance(clock, str) or not _CLOCK.match(clock):
                raise PhoneError("malformed", "time is HH:MM, 24-hour, in the phone's local time")
            clean["time"] = clock
            clean["repeats"] = repeats or None
    clean = {key: value for key, value in clean.items() if value is not None}
    return None, clean, alarm_summary({"kind": "timer" if kind == "set_timer" else "alarm", **clean})


def alarm_summary(item: dict[str, Any]) -> str:
    """One line: "Alarm Thu 16 Oct 06:30 · Wake up", "Timer 10 min · Pasta"."""
    label = item.get("label")
    if item.get("kind") == "timer":
        seconds = item.get("seconds") if item.get("seconds") is not None else item.get("remaining_s")
        head = f"Timer {format_duration(seconds)}" if isinstance(seconds, int) else "Timer"
    elif item.get("time"):
        head = f"Alarm {item['time']}" + (f" {format_days(item['repeats'])}" if item.get("repeats") else "")
    else:
        when = item.get("at") or item.get("fires_at")
        head = f"Alarm {format_when(when)}" if when else "Alarm"
        if item.get("repeats"):
            head += f" (repeats {format_days(item['repeats'])})"
    return f"{head} · {label}" if label else head


def format_duration(seconds: int) -> str:
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    parts = [f"{hours} h"] if hours else []
    if minutes:
        parts.append(f"{minutes} min")
    if secs or not parts:
        parts.append(f"{secs} s")
    return " ".join(parts)


def format_days(days: list[str]) -> str:
    chosen = [day for day in WEEKDAYS if day in days]
    if len(chosen) == 7:
        return "every day"
    if chosen == list(WEEKDAYS[:5]):
        return "weekdays"
    if chosen == list(WEEKDAYS[5:]):
        return "weekends"
    return ", ".join(day.capitalize() for day in chosen)


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
