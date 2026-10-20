"""Location from Conduit: the store, its encryption and its queries.

Conduit can share where the user is with this profile so Hermes can use it
(off until the user turns it on). Two layers, each its own opt-in on the
phone: "while I'm talking to Hermes" (a fix taken as a message is sent or a
voice call starts) and "in the background" (arriving at and leaving the
user's saved places, visits and significant moves). The phone decides the
detail before anything leaves it: places only, area (a neighbourhood and a
coordinate rounded to about a kilometre) or precise. This store enforces the
same rules again, so a phone bug can't store more than the user chose.

Saved places live on the phone. The host gets their names and kinds, never
their coordinates: each fix carries its distance to every saved place
instead, so "how far am I from home" works without the host knowing where
home is. A fix inside a place the user marked private arrives as that place's
id alone, and the agent hears only "at a private place".

What's kept: the latest fix per device, arrivals and departures for the
user's chosen history (latest only, a day, 7 days, 30 days or a year), a log
of every time the agent read any of it (shown in Conduit), place rules (run
a scheduled job when the user arrives somewhere or leaves) and on-demand
locate requests. Every ingest body is sealed end to end as in health_store:
each device agrees its own key with this profile (``/location/key``,
X25519), HKDF-SHA256 and ChaCha20-Poly1305, with location's own labels.

State lives in the profile's ``conduit-location.sqlite3`` (owner-only). The
dashboard routes (dashboard process) write it and the ``conduit_location``
tool and the call limits (agent process) read it: the dashboard loads this
file by path, so it imports nothing from the plugin package.
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

FILE_NAME = "conduit-location.sqlite3"
SCHEMA_VERSION = 3
# Versions _migrate brings up to SCHEMA_VERSION.
OLDER_SCHEMAS = ("1", "2")

SALT = b"conduit-location-v1"
INFO_INGEST = b"conduit-location-v1 ingest device-to-host"
# A live voice call reads the user's own location back (POST /voice-tools/call):
# each direction has its own key from the same agreement, apart from ingest.
INFO_VOICE_REQUEST = b"conduit-location-v1 voice device-to-host"
INFO_VOICE_ANSWER = b"conduit-location-v1 voice host-to-device"
# Maps lookups the phone answers with Apple Maps (conduit_maps): the host
# seals each request for one device, the device seals its answer back.
INFO_MAPS_REQUEST = b"conduit-location-v1 maps host-to-device"
INFO_MAPS_ANSWER = b"conduit-location-v1 maps device-to-host"
AAD_TAG = "conduit-location/1"
KID_TAG = b"conduit-location-kid\n"
NONCE_BYTES = 12
KEY_BYTES = 32

PRECISIONS = ("places", "area", "precise")
RETENTION_DAYS = (0, 1, 7, 30, 365)
PLACE_KINDS = ("home", "work", "gym", "school", "other")
# Layer 1 (talking) sources take the talking precision; the rest the
# background one.
TALKING_SOURCES = ("message", "call")
BACKGROUND_SOURCES = ("visit", "place", "significant", "app", "locate")
SOURCES = TALKING_SOURCES + BACKGROUND_SOURCES
EVENT_KINDS = ("arrive", "leave")
# What a place rule waits for: one of EVENT_KINDS, or either.
RULE_EVENTS = EVENT_KINDS + ("either",)
# A rule's place can be one saved place or any of them.
ANY_PLACE = "*"
# How often a rule may run: once a day, or every time (with a gap, so a
# phone hovering at a geofence's edge doesn't start a run per wobble).
RULE_REPEATS = ("daily", "every")
RULE_EVERY_MIN_GAP_S = 30 * 60
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

MAX_DEVICES = 4
MAX_PLACES = 20
MAX_EVENTS = 100
MAX_RULES = 20
MAX_NAME_CHARS = 40
MAX_DEVICE_NAME_CHARS = 60
MAX_AREA_CHARS = 80
MAX_TASK_CHARS = 600
MAX_WHAT_CHARS = 60
# Reads the user can look through in Conduit.
READS_KEPT_S = 30 * 24 * 3600
MAX_READS = 2000
# An arrival or departure that reaches the host later than this doesn't run
# rules: a brief for leaving home an hour ago is no use.
RULE_EVENT_MAX_AGE_S = 15 * 60
RULE_EVENT_SKEW_S = 2 * 60
# On-demand lookups: well under the phone's own allowance (about 360 a day).
LOCATE_MIN_GAP_S = 5 * 60
LOCATE_PER_DAY = 30
LOCATES_KEPT_S = 24 * 3600
# A fix older than this, with background sharing on, still says where the
# user is (the phone reports when they move); without it, a fix from a
# message says little after an hour.
TRUSTED_BACKGROUND_S = 24 * 3600
TRUSTED_TALKING_S = 3600
# What a phone says it can do, in its sealed sharing settings.
FEATURES = ("maps",)
# Maps lookups wake the phone the same way locate does (a location push),
# out of the same allowance: well under it, apart from locate's own budget.
MAPS_PER_HOUR = 30
MAPS_PER_DAY = 120
# A request the phone hasn't fetched by then is no use to the waiting tool.
MAPS_TTL_S = 60
# The same question within this long gets the last answer, no wake.
MAPS_CACHE_S = 120
MAPS_KEPT_S = 24 * 3600
MAX_MAPS_ANSWER_BYTES = 64 * 1024
MAX_MAPS_REQUEST_BYTES = 8 * 1024
EARLIEST = datetime(2020, 1, 1, tzinfo=timezone.utc)

_DEVICE_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}\Z")
_ITEM_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}\Z")
_RECORD_ID = re.compile(r"^[A-Za-z0-9:_-]{1,80}\Z")
_BATCH_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}\Z")
_LOCATE_ID = re.compile(r"^l_[A-Za-z0-9_-]{16,64}\Z")
_MAPS_ID = re.compile(r"^m_[A-Za-z0-9_-]{16,64}\Z")
_RULE_ID = re.compile(r"^r_[A-Za-z0-9_-]{8,40}\Z")
_KID = re.compile(r"^[0-9a-f]{32}\Z")
# A voice tool's name, bound into the sealed request and answer.
_VOICE_TOOL = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_B64URL = re.compile(r"^[A-Za-z0-9_-]*\Z")
_TZ = re.compile(r"^[A-Za-z0-9_+\-/]{1,64}\Z")
_COUNTRY = re.compile(r"^[A-Z]{2}\Z")
_HOURS = re.compile(r"^(\d{2}):(\d{2})-(\d{2}):(\d{2})\Z")
# Line breaks, other control characters and invisible format characters
# (bidi overrides, zero-width), which could make a place name read like
# instructions in the agent's tool result.
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2060-\u206f\ufeff]+")

_thread_lock = threading.Lock()


class LocationError(ValueError):
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
        raise LocationError("malformed", "malformed base64url")
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
        raise LocationError("bad_key", "That public key can't be used")
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


def maps_aad(direction: str, kid: str, device_id: str, request_id: str) -> bytes:
    """``direction`` is ``request`` (host to phone) or ``answer``."""
    return f"{AAD_TAG}\nmaps-{direction}\nkid={kid}\ndev={device_id}\nid={request_id}".encode("utf-8")


def voice_aad(direction: str, tool: str, kid: str, device_id: str, call_id: str) -> bytes:
    """``direction`` is "request" or "answer"; ``tool`` is the voice tool asked."""
    return "\n".join([AAD_TAG, f"voice-{direction}", f"tool={tool}", f"kid={kid}", f"dev={device_id}",
                      f"call={call_id}"]).encode("utf-8")


def seal(key: bytes, plaintext: bytes, aad: bytes, nonce: bytes | None = None) -> tuple[str, str]:
    """The phone's side, here for tests and the CLI's self-check."""
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    nonce = nonce or secrets.token_bytes(NONCE_BYTES)
    return b64u(nonce), b64u(ChaCha20Poly1305(key).encrypt(nonce, plaintext, aad))


def open_sealed(key: bytes, nonce: str, ciphertext: str, aad: bytes) -> bytes:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    raw_nonce = unb64u(nonce)
    if len(raw_nonce) != NONCE_BYTES:
        raise LocationError("malformed", "nonce must be 12 bytes")
    raw_ciphertext = unb64u(ciphertext)
    if len(raw_ciphertext) < 16:
        raise LocationError("malformed", "ct is shorter than its tag")
    try:
        return ChaCha20Poly1305(key).decrypt(raw_nonce, raw_ciphertext, aad)
    except InvalidTag:
        raise LocationError("bad_seal", "The body didn't open with this device's key; set up location sharing again",
                            status=409) from None


# --- Parsing helpers -------------------------------------------------------------


def parse_time(value: Any, field: str) -> datetime:
    """An ISO 8601 time with an offset (the phone's local offset is kept so
    "Tuesday 8 am" means the user's)."""
    if not isinstance(value, str) or len(value) > 40:
        raise LocationError("malformed", f"{field} must be an ISO 8601 time")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        raise LocationError("malformed", f"{field} must be an ISO 8601 time") from None
    if parsed.tzinfo is None:
        raise LocationError("malformed", f"{field} needs a UTC offset")
    if parsed < EARLIEST or parsed > datetime.now(timezone.utc) + timedelta(days=1):
        raise LocationError("malformed", f"{field} is out of range")
    return parsed


def iso_utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _number(value: Any, field: str, low: float, high: float) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise LocationError("malformed", f"{field} must be a finite number")
    if not low <= value <= high:
        raise LocationError("malformed", f"{field} is out of range")
    return float(value)


def clean_task(value: Any, limit: int) -> str | None:
    """A rule's task: like clean_text, but line breaks stay."""
    if not isinstance(value, str) or len(value) > limit:
        return None
    lines = (_CONTROL.sub(" ", line).strip() for line in value.replace("\r\n", "\n").split("\n"))
    return "\n".join(lines).strip() or None


def clean_text(value: Any, field: str, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise LocationError("malformed", f"{field} must be text of at most {limit} characters")
    # Double quotes become single ones: names are quoted in rule prompts,
    # and one mustn't be able to close that quote.
    return _CONTROL.sub(" ", value).replace('"', "'").strip() or None


def _bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise LocationError("malformed", f"{field} must be true or false")
    return value


def _list(value: Any, field: str, limit: int) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise LocationError("malformed", f"{field} must be a list")
    if len(value) > limit:
        raise LocationError("too_large", f"{field} holds at most {limit} items", status=413)
    return value


def parse_hours(value: Any) -> tuple[int, int] | None:
    """``HH:MM-HH:MM`` local time as minutes since midnight (end may be
    24:00; an end before the start wraps past midnight). None when
    malformed."""
    if not isinstance(value, str):
        return None
    match = _HOURS.match(value.strip())
    if not match:
        return None
    start_h, start_m, end_h, end_m = (int(part) for part in match.groups())
    if start_h > 23 or start_m > 59 or end_m > 59 or end_h > 24 or (end_h == 24 and end_m):
        return None
    start, end = start_h * 60 + start_m, end_h * 60 + end_m
    return None if start == end else (start, end)


def in_hours(minutes: int, hours: tuple[int, int] | None) -> bool:
    if hours is None:
        return True
    start, end = hours
    return start <= minutes < end if start < end else minutes >= start or minutes < end


# --- Store -----------------------------------------------------------------------


class LocationStore:
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
        self._upgrade()
        try:
            connection = sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True, timeout=15)
        except sqlite3.DatabaseError:
            # Deleted since the check above (a wipe).
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

    def _upgrade(self) -> None:
        """A store an older plugin wrote is brought up to date before it's
        read, so updating the plugin doesn't hide what's shared until the
        phone's next upload."""
        try:
            with contextlib.closing(sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True, timeout=15)) as db:
                if _meta(db, "schema") not in OLDER_SCHEMAS:
                    return
        except sqlite3.DatabaseError:
            return
        with self._write():
            pass

    # Keys ------------------------------------------------------------------------

    def register_key(self, body: Any) -> dict[str, Any]:
        """A device's X25519 public key in, this profile's for that device
        out. Replaces the device's earlier key (the phone re-keys after a
        reinstall or when the user turns sharing off and on). Registering
        the same device key keeps its latest fix and batch watermark."""
        if not isinstance(body, dict):
            raise LocationError("malformed", "Expected a JSON object")
        device_id = body.get("device_id")
        if not isinstance(device_id, str) or not _DEVICE_ID.match(device_id):
            raise LocationError("malformed", "device_id must be 8-64 letters, digits, - or _")
        name = clean_text(body.get("device_name"), "device_name", MAX_DEVICE_NAME_CHARS) or "iPhone"
        device_public = unb64u(body.get("public_key"))
        if len(device_public) != 32:
            raise LocationError("malformed", "public_key must be a 32-byte X25519 key")
        if not crypto_available():
            raise LocationError("no_crypto", "Location sharing needs the cryptography package on this host",
                                status=501)
        host_private, host_public = new_host_keypair()
        agree(host_private, device_public)  # refuses a low-order key now, not at the first ingest
        kid = key_id(device_public, host_public)
        now = int(self.clock())
        with self._write() as db:
            known = db.execute("SELECT device_public FROM devices WHERE id=?", (device_id,)).fetchone()
            count = db.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
            if not known and count >= MAX_DEVICES:
                raise LocationError("too_many_devices",
                                    f"This profile already keeps location from {MAX_DEVICES} devices; delete one "
                                    "from Conduit first", status=409)
            db.execute(
                "INSERT INTO devices(id, name, device_public, host_private, host_public, kid, created_at)"
                " VALUES(?,?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET name=excluded.name, device_public=excluded.device_public,"
                " host_private=excluded.host_private, host_public=excluded.host_public, kid=excluded.kid,"
                " created_at=excluded.created_at,"
                " last_sent=CASE WHEN devices.device_public=excluded.device_public THEN devices.last_sent END",
                (device_id, name, device_public, host_private, host_public, kid, now),
            )
            # A new key means a reinstall or sharing turned off and on: what
            # this device said before is no longer where the user is.
            if known and bytes(known[0]) != device_public:
                db.execute("DELETE FROM fixes WHERE device=?", (device_id,))
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
            raise LocationError("unknown_key", "This device's location key isn't on this profile; set it up again",
                                status=409)

    def _device_key(self, db: sqlite3.Connection, device_id: str, kid: str, purpose: bytes = INFO_INGEST) -> bytes:
        row = db.execute("SELECT device_public, host_private, host_public, kid FROM devices WHERE id=?",
                         (device_id,)).fetchone()
        if row is None or row[3] != kid:
            raise LocationError("unknown_key", "This device's location key isn't on this profile; set it up again",
                                status=409)
        device_public, host_private, host_public = bytes(row[0]), bytes(row[1]), bytes(row[2])
        return derive_key(agree(host_private, device_public), device_id, device_public, host_public, purpose)

    # Voice reads ---------------------------------------------------------------------

    def open_voice_request(self, envelope: Any) -> tuple[str, dict[str, Any], Callable[[Any], dict[str, str]]]:
        """A live voice call's sealed read from one device: its device id, the
        opened request, and a function that seals the answer for that device
        alone. Only a device holding its own key can ask, and the caller reads
        only that device's location (``snapshot(device_id)``)."""
        if not isinstance(envelope, dict) or type(envelope.get("v")) is not int or envelope.get("v") != 1:
            raise LocationError("malformed", "Expected a version 1 sealed request")
        device_id, kid, call_id = envelope.get("device_id"), envelope.get("kid"), envelope.get("call_id")
        tool = envelope.get("tool")
        if not isinstance(tool, str) or not _VOICE_TOOL.match(tool):
            raise LocationError("malformed", "tool is malformed")
        if not isinstance(device_id, str) or not _DEVICE_ID.match(device_id):
            raise LocationError("malformed", "device_id is malformed")
        if not isinstance(kid, str) or not _KID.match(kid):
            raise LocationError("malformed", "kid is malformed")
        if not isinstance(call_id, str) or not _BATCH_ID.match(call_id):
            raise LocationError("malformed", "call_id is malformed")
        if not crypto_available():
            raise LocationError("no_crypto", "Location sharing needs the cryptography package on this host",
                                status=501)
        with self._read() as db:
            if db is None:
                raise LocationError("unknown_key",
                                    "This device's location key isn't on this profile; set it up again", status=409)
            request_key = self._device_key(db, device_id, kid, INFO_VOICE_REQUEST)
            answer_key = self._device_key(db, device_id, kid, INFO_VOICE_ANSWER)
        plaintext = open_sealed(request_key, envelope.get("nonce"), envelope.get("ct"),
                                voice_aad("request", tool, kid, device_id, call_id))
        try:
            request = json.loads(plaintext)
        except (ValueError, RecursionError):
            raise LocationError("malformed", "The sealed request isn't JSON") from None
        if not isinstance(request, dict) or request.get("call_id") != call_id:
            raise LocationError("malformed", "The sealed request doesn't match its envelope")

        def seal_answer(answer: Any) -> dict[str, str]:
            self._still_keyed(device_id, kid)
            body = json.dumps({"call_id": call_id, "result": answer}, ensure_ascii=False).encode("utf-8")
            nonce, ct = seal(answer_key, body, voice_aad("answer", tool, kid, device_id, call_id))
            return {"nonce": nonce, "ct": ct}

        return device_id, request, seal_answer

    # Ingest --------------------------------------------------------------------

    def ingest(self, envelope: Any) -> dict[str, Any]:
        """A sealed batch from a device. ``rules_fired`` lists the place
        rules an arrival or departure in it set off (the route starts their
        jobs once this has committed)."""
        if not isinstance(envelope, dict) or type(envelope.get("v")) is not int or envelope.get("v") != 1:
            raise LocationError("malformed", "Expected a version 1 sealed batch")
        device_id, kid, batch_id = envelope.get("device_id"), envelope.get("kid"), envelope.get("batch_id")
        if not isinstance(device_id, str) or not _DEVICE_ID.match(device_id):
            raise LocationError("malformed", "device_id is malformed")
        if not isinstance(kid, str) or not _KID.match(kid):
            raise LocationError("malformed", "kid is malformed")
        if not isinstance(batch_id, str) or not _BATCH_ID.match(batch_id):
            raise LocationError("malformed", "batch_id is malformed")
        if not crypto_available():
            raise LocationError("no_crypto", "Location sharing needs the cryptography package on this host",
                                status=501)
        now = self.clock()
        with self._write() as db:
            key = self._device_key(db, device_id, kid)
            plaintext = open_sealed(key, envelope.get("nonce"), envelope.get("ct"),
                                    ingest_aad(kid, device_id, batch_id))
            try:
                batch = json.loads(plaintext)
            except (ValueError, RecursionError):
                raise LocationError("malformed", "The sealed batch isn't JSON") from None
            if not isinstance(batch, dict) or batch.get("batch_id") != batch_id:
                raise LocationError("malformed", "The sealed batch doesn't match its envelope")
            result = _apply(db, device_id, batch, now)
            # Every upload also forgets old lookups, so they don't outlive
            # their window when no new lookup comes.
            _prune_maps(db, now)
            generation = _meta(db, "generation")
        return {**result, "generation": generation}

    # Status, log and wipe -------------------------------------------------------------

    def status(self, device_id: Any = None) -> dict[str, Any]:
        with self._read() as db:
            if db is None:
                return {"stored": False, "generation": None, "device": None, "reads_24h": 0, "rules": 0}
            now = self.clock()
            answer: dict[str, Any] = {
                "stored": True, "generation": _meta(db, "generation"), "device": None,
                "reads_24h": db.execute("SELECT COUNT(*) FROM reads WHERE at>?", (now - 86400,)).fetchone()[0],
                "rules": db.execute("SELECT COUNT(*) FROM rules").fetchone()[0],
            }
            if isinstance(device_id, str) and _DEVICE_ID.match(device_id):
                row = db.execute("SELECT kid, last_seen, sharing FROM devices WHERE id=?", (device_id,)).fetchone()
                if row is not None:
                    answer["device"] = {"kid": row[0], "last_sync": iso_utc(row[1]) if row[1] else None,
                                        "sharing": _decode(row[2], None)}
            return answer

    def reads(self, limit: int = 50) -> list[dict[str, Any]]:
        """The agent's reads, newest first: when, what and in which chat.
        Never what it read."""
        with self._read() as db:
            if db is None:
                return []
            return [{"at": iso_utc(at), "action": action, "session_title": title or None}
                    for at, action, title in db.execute(
                        "SELECT at, action, title FROM reads WHERE at>? ORDER BY at DESC LIMIT ?",
                        (self.clock() - READS_KEPT_S, max(1, min(int(limit), 200))))]

    def log_read(self, action: str, session_id: str, title: str) -> None:
        now = self.clock()
        with self._write() as db:
            db.execute("INSERT INTO reads(at, action, session, title) VALUES(?,?,?,?)",
                       (now, action, session_id[:180], (title or "")[:120]))
            db.execute("DELETE FROM reads WHERE at<?", (now - READS_KEPT_S,))
            db.execute("DELETE FROM reads WHERE rowid NOT IN (SELECT rowid FROM reads ORDER BY at DESC LIMIT ?)",
                       (MAX_READS,))

    def wipe(self, device_id: Any = None) -> dict[str, Any]:
        """Deletes one device's location data and key, or everything (rules
        and the read log too) when ``device_id`` is None. The file goes once
        nothing is left."""
        if device_id is not None and (not isinstance(device_id, str) or not _DEVICE_ID.match(device_id)):
            raise LocationError("malformed", "device_id is malformed")
        if not self.path.exists():
            return {"deleted": True, "remaining_devices": 0, "rule_jobs": []}
        with self._write() as db:
            jobs: list[str] = []
            for table in ("fixes", "visits", "places", "maps", "devices"):
                column = "id" if table == "devices" else "device"
                if device_id is None:
                    db.execute(f"DELETE FROM {table}")
                else:
                    db.execute(f"DELETE FROM {table} WHERE {column}=?", (device_id,))
            if device_id is None:
                jobs = [row[0] for row in db.execute("SELECT job_id FROM rules WHERE job_id IS NOT NULL")]
                for table in ("rules", "reads", "locates"):
                    db.execute(f"DELETE FROM {table}")
        with _thread_lock:
            remaining = self._count("devices")
            if remaining == 0 and self._count("rules") == 0:
                for suffix in ("", "-wal", "-shm", "-journal"):
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(f"{self.path}{suffix}")
        return {"deleted": True, "remaining_devices": remaining, "rule_jobs": jobs}

    def _count(self, table: str) -> int:
        """Rows in a table; the caller holds ``_thread_lock``."""
        if not self.path.exists():
            return 0
        connection = sqlite3.connect(self.path, timeout=15)
        try:
            return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.DatabaseError:
            return 0
        finally:
            connection.close()

    # The agent's view -------------------------------------------------------------------

    def snapshot(self, device_id: str | None = None) -> dict[str, Any] | None:
        """Where the user is: the device with the newest fix, with its sharing
        and places, or None when nothing is shared. The device that synced
        last decides when it says sharing is paused or off (the user's
        latest word), and when no device has a fix. ``device_id`` (a voice
        read) reads that device alone, matched as an id."""
        with self._read() as db:
            if db is None:
                return None
            if device_id is not None:
                rows = db.execute("SELECT id, name, last_seen, sharing FROM devices WHERE id=? AND last_seen IS NOT"
                                  " NULL", (device_id,)).fetchall()
            else:
                rows = db.execute("SELECT id, name, last_seen, sharing FROM devices WHERE last_seen IS NOT NULL"
                                  " ORDER BY last_seen DESC").fetchall()
            if not rows:
                return None
            row = rows[0]
            latest = _decode(row[3], None) or {}
            if device_id is None and not sharing_paused(latest, self.clock()) and (
                    latest.get("talking") or latest.get("background")):
                newest = db.execute(
                    "SELECT device FROM fixes WHERE device IN (%s) ORDER BY at DESC LIMIT 1"
                    % ",".join("?" * len(rows)), [item[0] for item in rows]).fetchone()
                if newest is not None:
                    candidate = next(item for item in rows if item[0] == newest[0])
                    sharing = _decode(candidate[3], None) or {}
                    if not sharing_paused(sharing, self.clock()):
                        row = candidate
            device_id = row[0]
            places = {place_id: {"id": place_id, "name": name, "kind": kind, "private": bool(private)}
                      for place_id, name, kind, private in db.execute(
                          "SELECT id, name, kind, private FROM places WHERE device=? ORDER BY position", (device_id,))}
            fix = db.execute(
                "SELECT id, at, at_local, source, precision, lat, lon, accuracy, place_id, area, distances, speed,"
                " locate_id FROM fixes WHERE device=?", (device_id,)).fetchone()
            latest = None
            if fix is not None:
                latest = {"id": fix[0], "at": fix[1], "at_local": fix[2], "source": fix[3], "precision": fix[4],
                          "lat": fix[5], "lon": fix[6], "accuracy_m": fix[7], "place_id": fix[8],
                          "area": _decode(fix[9], None), "distances_m": _decode(fix[10], {}),
                          "speed_mps": fix[11], "locate_id": fix[12]}
            return {"device_id": device_id, "device_name": row[1], "last_sync": row[2],
                    "sharing": _decode(row[3], None) or {}, "places": places, "fix": latest}

    def history(self, device_id: str, start: float, end: float, limit: int) -> tuple[list[dict[str, Any]], bool]:
        with self._read() as db:
            if db is None:
                return [], False
            rows = db.execute(
                "SELECT kind, place_id, place_name, area, at_local FROM visits WHERE device=? AND at>=? AND at<?"
                " ORDER BY at LIMIT ?", (device_id, start, end, limit + 1)).fetchall()
        found = [{"at": at_local, "event": "arrived" if kind == "arrive" else "left",
                  **({"place": place_name} if place_name else {}), **({"area": area} if area else {})}
                 for kind, _, place_name, area, at_local in rows]
        return found[:limit], len(found) > limit

    def latest_place(self) -> dict[str, Any] | None:
        """Where the user is for call limits: ``{"place_id", "trusted",
        "paused"}``, or None when nothing is shared. ``trusted`` is False
        when the latest fix is too old to say (see TRUSTED_*)."""
        view = self.snapshot()
        if view is None:
            return None
        now = self.clock()
        sharing = view["sharing"]
        paused = sharing_paused(sharing, now)
        fix = view["fix"]
        if fix is None or paused:
            return {"place_id": None, "trusted": False, "paused": paused}
        window = TRUSTED_BACKGROUND_S if sharing.get("background") else TRUSTED_TALKING_S
        return {"place_id": fix["place_id"], "trusted": now - fix["at"] <= window, "paused": False}

    # Rules ----------------------------------------------------------------------------

    def rules(self) -> list[dict[str, Any]]:
        with self._read() as db:
            if db is None:
                return []
            return [_rule_row(row) for row in db.execute(f"SELECT {_RULE_COLUMNS} FROM rules ORDER BY created_at")]

    def put_rule(self, rule: dict[str, Any], *, replacing: bool = False) -> dict[str, Any]:
        """Adds or replaces a rule (by id). The caller made its job first.
        ``replacing``: the caller is changing a rule it found, which must
        still be there (the user may have just deleted it in Conduit)."""
        now = self.clock()
        with self._write() as db:
            exists = db.execute("SELECT 1 FROM rules WHERE id=?", (rule["id"],)).fetchone()
            if replacing and not exists:
                raise LocationError("rule_gone", "That place rule was just deleted", status=409)
            if not exists and db.execute("SELECT COUNT(*) FROM rules").fetchone()[0] >= MAX_RULES:
                raise LocationError("too_many_rules", f"There are already {MAX_RULES} place rules", status=409)
            db.execute(
                "INSERT INTO rules(id, job_id, what, place_id, place_name, event, days, hours, task, call, repeat,"
                " created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET job_id=excluded.job_id,"
                " what=excluded.what, place_id=excluded.place_id, place_name=excluded.place_name,"
                " event=excluded.event, days=excluded.days, hours=excluded.hours, task=excluded.task,"
                " call=excluded.call, repeat=excluded.repeat",
                (rule["id"], rule["job_id"], rule["what"], rule["place_id"], rule["place_name"], rule["event"],
                 ",".join(rule["days"]), rule["hours"] or None, rule["task"], 1 if rule["call"] else 0,
                 rule.get("repeat") if rule.get("repeat") in RULE_REPEATS else "daily", now))
            row = db.execute(f"SELECT {_RULE_COLUMNS} FROM rules WHERE id=?", (rule["id"],)).fetchone()
        return _rule_row(row)

    def remove_rule(self, rule_id: Any) -> str | None:
        """Removes a rule; its job's id (for the caller to remove), or None
        when there was no such rule."""
        if not isinstance(rule_id, str) or not _RULE_ID.match(rule_id) or not self.path.exists():
            return None
        with self._write() as db:
            row = db.execute("SELECT job_id FROM rules WHERE id=?", (rule_id,)).fetchone()
            if row is None:
                return None
            db.execute("DELETE FROM rules WHERE id=?", (rule_id,))
            return row[0] or ""

    # On-demand locate -------------------------------------------------------------------

    def start_locate(self) -> str:
        """A new locate request id, within the budget. Raises LocationError
        ``too_soon`` / ``daily_limit``."""
        now = self.clock()
        with self._write() as db:
            db.execute("DELETE FROM locates WHERE requested_at<?", (now - LOCATES_KEPT_S,))
            times = [row[0] for row in db.execute("SELECT requested_at FROM locates ORDER BY requested_at DESC")]
            if times and now - times[0] < LOCATE_MIN_GAP_S:
                wait = int(LOCATE_MIN_GAP_S - (now - times[0])) // 60 + 1
                raise LocationError("too_soon", f"A fresh location was asked for moments ago; try again in about "
                                                f"{wait} minutes, or use action now", status=429)
            if len(times) >= LOCATE_PER_DAY:
                raise LocationError("daily_limit", "The day's fresh lookups are used up; use action now",
                                    status=429)
            locate_id = "l_" + secrets.token_urlsafe(18)
            db.execute("INSERT INTO locates(id, requested_at) VALUES(?,?)", (locate_id, now))
        return locate_id

    def cancel_locate(self, locate_id: str) -> None:
        """A request the relay couldn't send doesn't count against the
        budget."""
        with self._write() as db:
            db.execute("DELETE FROM locates WHERE id=? AND answered_at IS NULL", (locate_id,))

    def locate_answered(self, locate_id: str) -> bool:
        with self._read() as db:
            if db is None:
                return False
            row = db.execute("SELECT answered_at FROM locates WHERE id=?", (locate_id,)).fetchone()
            return bool(row and row[0])


    # Maps lookups on the phone ---------------------------------------------------------

    def maps_device(self) -> dict[str, Any] | None:
        """The device that answers maps lookups: background sharing on, not
        paused, and a Conduit that says it can (``features``), newest sync
        first. None when no device can."""
        now = self.clock()
        with self._read() as db:
            if db is None:
                return None
            for device_id, kid, sharing_text in db.execute(
                    "SELECT id, kid, sharing FROM devices WHERE last_seen IS NOT NULL ORDER BY last_seen DESC"):
                sharing = _decode(sharing_text, None) or {}
                if (sharing.get("background") and "maps" in (sharing.get("features") or [])
                        and not sharing_paused(sharing, now)):
                    return {"device_id": device_id, "kid": kid, "sharing": sharing}
        return None

    def maps_host_allowed(self) -> bool:
        """False while any device asks to keep maps lookups on the phone."""
        with self._read() as db:
            if db is None:
                return True
            return all((_decode(text, None) or {}).get("maps_host", True) is not False
                       for (text,) in db.execute("SELECT sharing FROM devices WHERE sharing IS NOT NULL"))

    def start_maps(self, device_id: str, request: dict[str, Any]) -> dict[str, Any]:
        """Stores a lookup for one device. Returns ``{"cached": answer}``
        when the same question was answered moments ago, else ``{"id": …}``
        (the caller wakes the phone). Raises LocationError ``too_soon`` /
        ``daily_limit`` past the budget."""
        cache = json.dumps(request, sort_keys=True, separators=(",", ":"))
        if len(cache) > MAX_MAPS_REQUEST_BYTES:
            raise LocationError("malformed", "That lookup is too long")
        now = self.clock()
        with self._write() as db:
            _prune_maps(db, now)
            row = db.execute(
                "SELECT answer FROM maps WHERE device=? AND cache=? AND answered_at>? AND answer IS NOT NULL"
                " ORDER BY answered_at DESC LIMIT 1", (device_id, cache, now - MAPS_CACHE_S)).fetchone()
            if row is not None:
                answer = _decode(row[0], None)
                if isinstance(answer, dict) and answer.get("ok"):
                    return {"cached": answer}
            hour = db.execute("SELECT COUNT(*) FROM maps WHERE requested_at>?", (now - 3600,)).fetchone()[0]
            day = db.execute("SELECT COUNT(*) FROM maps WHERE requested_at>?", (now - 86400,)).fetchone()[0]
            if day >= MAPS_PER_DAY:
                raise LocationError("daily_limit", "The day's maps lookups on the phone are used up", status=429)
            if hour >= MAPS_PER_HOUR:
                raise LocationError("too_soon", "Too many maps lookups this hour; try again later", status=429)
            request_id = "m_" + secrets.token_urlsafe(18)
            body = json.dumps({**request, "id": request_id,
                               "expires_at": iso_utc(now + MAPS_TTL_S)}, ensure_ascii=False)
            db.execute("INSERT INTO maps(id, device, requested_at, expires_at, cache, request) VALUES(?,?,?,?,?,?)",
                       (request_id, device_id, now, now + MAPS_TTL_S, cache, body))
        return {"id": request_id}

    def cancel_maps(self, request_id: str) -> None:
        """A lookup the relay couldn't deliver doesn't count against the
        budget, and the phone can't fetch it any more."""
        with self._write() as db:
            db.execute("DELETE FROM maps WHERE id=? AND answered_at IS NULL", (request_id,))

    def maps_request(self, request_id: Any, device_id: Any, kid: Any) -> dict[str, Any]:
        """The sealed lookup for the device it was made for (GET
        /location/maps/{id}): only while it's waiting and unexpired."""
        if not isinstance(request_id, str) or not _MAPS_ID.match(request_id):
            raise LocationError("malformed", "The lookup id is malformed")
        if not isinstance(device_id, str) or not _DEVICE_ID.match(device_id):
            raise LocationError("malformed", "device_id is malformed")
        if not isinstance(kid, str) or not _KID.match(kid):
            raise LocationError("malformed", "kid is malformed")
        if not crypto_available():
            raise LocationError("no_crypto", "Location sharing needs the cryptography package on this host",
                                status=501)
        now = self.clock()
        with self._write() as db:
            row = db.execute("SELECT request FROM maps WHERE id=? AND device=? AND answered_at IS NULL"
                             " AND expires_at>?", (request_id, device_id, now)).fetchone()
            if row is None:
                raise LocationError("unknown", "No waiting lookup has that id", status=404)
            key = self._device_key(db, device_id, kid, INFO_MAPS_REQUEST)
            nonce, ct = seal(key, row[0].encode("utf-8"), maps_aad("request", kid, device_id, request_id))
            db.execute("UPDATE maps SET fetched_at=? WHERE id=?", (now, request_id))
        return {"v": 1, "nonce": nonce, "ct": ct}

    def answer_maps(self, request_id: Any, envelope: Any) -> dict[str, Any]:
        """The phone's sealed answer (POST /location/maps/{id})."""
        if not isinstance(request_id, str) or not _MAPS_ID.match(request_id):
            raise LocationError("malformed", "The lookup id is malformed")
        if not isinstance(envelope, dict) or type(envelope.get("v")) is not int or envelope.get("v") != 1:
            raise LocationError("malformed", "Expected a version 1 sealed answer")
        device_id, kid = envelope.get("device_id"), envelope.get("kid")
        if not isinstance(device_id, str) or not _DEVICE_ID.match(device_id):
            raise LocationError("malformed", "device_id is malformed")
        if not isinstance(kid, str) or not _KID.match(kid):
            raise LocationError("malformed", "kid is malformed")
        if not crypto_available():
            raise LocationError("no_crypto", "Location sharing needs the cryptography package on this host",
                                status=501)
        now = self.clock()
        with self._write() as db:
            # After expiry the tool has long stopped waiting; a late answer
            # would only reach the repeat cache.
            row = db.execute("SELECT 1 FROM maps WHERE id=? AND device=? AND answered_at IS NULL AND expires_at>?",
                             (request_id, device_id, now)).fetchone()
            if row is None:
                raise LocationError("unknown", "No waiting lookup has that id", status=404)
            key = self._device_key(db, device_id, kid, INFO_MAPS_ANSWER)
            plaintext = open_sealed(key, envelope.get("nonce"), envelope.get("ct"),
                                    maps_aad("answer", kid, device_id, request_id))
            if len(plaintext) > MAX_MAPS_ANSWER_BYTES:
                raise LocationError("malformed", "The answer is too long")
            try:
                answer = json.loads(plaintext)
            except (ValueError, RecursionError):
                raise LocationError("malformed", "The sealed answer isn't JSON") from None
            if not isinstance(answer, dict) or answer.get("id") != request_id or not isinstance(answer.get("ok"), bool):
                raise LocationError("malformed", "The sealed answer doesn't match its request")
            db.execute("UPDATE maps SET answered_at=?, answer=? WHERE id=?",
                       (now, json.dumps(answer, ensure_ascii=False), request_id))
        return {}

    def maps_answer(self, request_id: str) -> dict[str, Any] | None:
        with self._read() as db:
            if db is None:
                return None
            row = db.execute("SELECT answer FROM maps WHERE id=? AND answered_at IS NOT NULL",
                             (request_id,)).fetchone()
            answer = _decode(row[0], None) if row else None
            return answer if isinstance(answer, dict) else None


def sharing_paused(sharing: dict[str, Any], now: float) -> bool:
    """True while the user paused sharing, or has both layers off."""
    if not sharing.get("talking") and not sharing.get("background"):
        return True
    until = sharing.get("paused_until")
    if isinstance(until, str):
        try:
            return datetime.fromisoformat(until).timestamp() > now
        except ValueError:
            # Stored by _parse_sharing, so this is a damaged file: paused is
            # the safe reading.
            return True
    return False


_RULE_COLUMNS = ("id, job_id, what, place_id, place_name, event, days, hours, task, call, last_fired_day, last_fired_at,"
                 " repeat")


def _rule_row(row: tuple[Any, ...]) -> dict[str, Any]:
    return {"id": row[0], "job_id": row[1], "what": row[2], "place_id": row[3], "place_name": row[4],
            "event": row[5], "days": [day for day in (row[6] or "").split(",") if day], "hours": row[7] or "",
            "task": row[8], "call": bool(row[9]), "last_fired": row[10] or None,
            "last_fired_at": iso_utc(row[11]) if row[11] else None, "last_fired_ts": row[11],
            "repeat": row[12] if row[12] in RULE_REPEATS else "daily"}


# --- Schema and batch apply -----------------------------------------------------------


def _meta(db: sqlite3.Connection, key: str) -> str | None:
    try:
        row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else None


# Maps requests (schema 2): the sealed-for-one-device question, its answer,
# and `cache` (the question without its id) to answer a repeat from here.
# Sealing covers the trip to and from the phone; here, in this owner-only
# file, the question and answer stay readable while they're useful (the
# request's minute plus the repeat window, about 3 minutes), then are
# blanked. The row (id, device, times) stays a day for the budget.
_MAPS_TABLE = """
    CREATE TABLE IF NOT EXISTS maps(
        id TEXT PRIMARY KEY, device TEXT NOT NULL, requested_at REAL NOT NULL, expires_at REAL NOT NULL,
        cache TEXT NOT NULL, request TEXT NOT NULL, fetched_at REAL, answered_at REAL, answer TEXT)
"""


def _prune_maps(db: sqlite3.Connection, now: float) -> None:
    """Rows stay a day for the budget; what was asked and found goes once no
    one can use it."""
    db.execute("DELETE FROM maps WHERE requested_at<?", (now - MAPS_KEPT_S,))
    db.execute("UPDATE maps SET cache='', request='', answer=NULL WHERE requested_at<? AND request!=''",
               (now - MAPS_TTL_S - MAPS_CACHE_S,))


def _migrate(db: sqlite3.Connection, now: float) -> None:
    version = _meta(db, "schema")
    if version == str(SCHEMA_VERSION):
        return
    if version in OLDER_SCHEMAS:
        if version == "1":
            db.execute(_MAPS_TABLE)
        # 3: when the newest batch was sent, so a late older one changes nothing.
        db.execute("ALTER TABLE devices ADD COLUMN last_sent REAL")
        db.execute("UPDATE meta SET value=? WHERE key='schema'", (str(SCHEMA_VERSION),))
        return
    if version is not None:
        raise LocationError("schema", "This location store was written by a newer plugin; update the plugin",
                            status=409)
    # One statement at a time: executescript would commit the open transaction.
    for statement in """
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS devices(
            id TEXT PRIMARY KEY, name TEXT NOT NULL,
            device_public BLOB NOT NULL, host_private BLOB NOT NULL, host_public BLOB NOT NULL, kid TEXT NOT NULL,
            created_at INTEGER NOT NULL, last_seen REAL, sharing TEXT, last_sent REAL);
        CREATE TABLE IF NOT EXISTS places(
            device TEXT NOT NULL, id TEXT NOT NULL, position INTEGER NOT NULL, name TEXT NOT NULL,
            kind TEXT NOT NULL, private INTEGER NOT NULL, PRIMARY KEY(device, id));
        CREATE TABLE IF NOT EXISTS fixes(
            device TEXT PRIMARY KEY, id TEXT NOT NULL, at REAL NOT NULL, at_local TEXT NOT NULL,
            source TEXT NOT NULL, precision TEXT NOT NULL, lat REAL, lon REAL, accuracy REAL, place_id TEXT,
            area TEXT, distances TEXT, speed REAL, locate_id TEXT, received_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS visits(
            device TEXT NOT NULL, id TEXT NOT NULL, kind TEXT NOT NULL, place_id TEXT, place_name TEXT,
            area TEXT, at REAL NOT NULL, at_local TEXT NOT NULL, received_at REAL NOT NULL,
            PRIMARY KEY(device, id));
        CREATE INDEX IF NOT EXISTS visits_by_time ON visits(device, at);
        CREATE TABLE IF NOT EXISTS reads(at REAL NOT NULL, action TEXT NOT NULL, session TEXT, title TEXT);
        CREATE INDEX IF NOT EXISTS reads_by_time ON reads(at);
        CREATE TABLE IF NOT EXISTS rules(
            id TEXT PRIMARY KEY, job_id TEXT, what TEXT NOT NULL, place_id TEXT NOT NULL, place_name TEXT NOT NULL,
            event TEXT NOT NULL, days TEXT NOT NULL, hours TEXT, task TEXT NOT NULL, call INTEGER NOT NULL,
            repeat TEXT NOT NULL DEFAULT 'daily', created_at REAL NOT NULL, last_fired_day TEXT,
            last_fired_at REAL);
        CREATE TABLE IF NOT EXISTS locates(id TEXT PRIMARY KEY, requested_at REAL NOT NULL, answered_at REAL)
    """.split(";"):
        if statement.strip():
            db.execute(statement)
    db.execute(_MAPS_TABLE)
    db.execute("INSERT OR REPLACE INTO meta VALUES('schema', ?)", (str(SCHEMA_VERSION),))
    db.execute("INSERT OR IGNORE INTO meta VALUES('generation', ?)", (secrets.token_hex(8),))
    db.execute("INSERT OR IGNORE INTO meta VALUES('created_at', ?)", (str(int(now)),))


def _parse_sharing(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LocationError("malformed", "sharing must be an object")
    sharing: dict[str, Any] = {}
    for layer in ("talking", "background"):
        sharing[layer] = _bool(value.get(layer), f"sharing.{layer}")
        precision = value.get(f"{layer}_precision")
        if precision not in PRECISIONS:
            raise LocationError("malformed", f"sharing.{layer}_precision must be one of {', '.join(PRECISIONS)}")
        sharing[f"{layer}_precision"] = precision
    retention = value.get("retention_days")
    if isinstance(retention, bool) or retention not in RETENTION_DAYS:
        raise LocationError("malformed", "sharing.retention_days must be 0, 1, 7, 30 or 365")
    sharing["retention_days"] = retention
    paused = value.get("paused_until")
    if paused is not None:
        if not isinstance(paused, str) or len(paused) > 40:
            raise LocationError("malformed", "sharing.paused_until must be an ISO 8601 time or null")
        try:
            parsed = datetime.fromisoformat(paused[:-1] + "+00:00" if paused.endswith("Z") else paused)
        except ValueError:
            raise LocationError("malformed", "sharing.paused_until must be an ISO 8601 time or null") from None
        if parsed.tzinfo is None:
            raise LocationError("malformed", "sharing.paused_until needs a UTC offset")
        # "Until I turn it back on" is far ahead; stored as given.
        sharing["paused_until"] = parsed.isoformat()
    else:
        sharing["paused_until"] = None
    zone = value.get("time_zone")
    if zone is not None and (not isinstance(zone, str) or not _TZ.match(zone)):
        raise LocationError("malformed", "sharing.time_zone is malformed")
    sharing["time_zone"] = zone
    # What this Conduit can do (newer phones only): unknown entries are
    # ignored, so a later phone doesn't break an older plugin.
    features = value.get("features")
    if features is not None and not isinstance(features, list):
        raise LocationError("malformed", "sharing.features must be a list")
    sharing["features"] = sorted({item for item in features or [] if item in FEATURES})
    # "Keep maps lookups on this iPhone": false keeps conduit_maps off this
    # host's Apple Maps engine.
    maps_host = value.get("maps_host", True)
    if not isinstance(maps_host, bool):
        raise LocationError("malformed", "sharing.maps_host must be true or false")
    sharing["maps_host"] = maps_host
    return sharing


def _parse_places(value: Any) -> list[dict[str, Any]]:
    places = _list(value, "places", MAX_PLACES)
    parsed, seen = [], set()
    for index, place in enumerate(places):
        field = f"places[{index}]"
        if not isinstance(place, dict):
            raise LocationError("malformed", f"{field} must be an object")
        place_id = place.get("id")
        if not isinstance(place_id, str) or not _ITEM_ID.match(place_id) or place_id in seen:
            raise LocationError("malformed", f"{field}.id is malformed or repeated")
        seen.add(place_id)
        name = clean_text(place.get("name"), f"{field}.name", MAX_NAME_CHARS)
        if not name:
            raise LocationError("malformed", f"{field}.name is required")
        if place.get("kind") not in PLACE_KINDS:
            raise LocationError("malformed", f"{field}.kind must be one of {', '.join(PLACE_KINDS)}")
        if any(key in place for key in ("lat", "lon", "latitude", "longitude")):
            # Saved places stay on the phone: the host never learns where
            # home is.
            raise LocationError("malformed", f"{field} must not carry coordinates")
        parsed.append({"id": place_id, "name": name, "kind": place["kind"],
                       "private": _bool(place.get("private", False), f"{field}.private")})
    return parsed


def _parse_area(value: Any, field: str) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise LocationError("malformed", f"{field} must be an object")
    area: dict[str, str] = {}
    for key in ("name", "locality", "region", "country"):
        text = clean_text(value.get(key), f"{field}.{key}", MAX_AREA_CHARS)
        if text:
            area[key] = text
    code = value.get("country_code")
    if code is not None:
        if not isinstance(code, str) or not _COUNTRY.match(code):
            raise LocationError("malformed", f"{field}.country_code is two capital letters")
        area["country_code"] = code
    return area or None


def _parse_fix(value: Any, sharing: dict[str, Any], places: dict[str, dict[str, Any]]) -> dict[str, Any]:
    field = "fix"
    if not isinstance(value, dict):
        raise LocationError("malformed", f"{field} must be an object")
    fix_id = value.get("id")
    if not isinstance(fix_id, str) or not _RECORD_ID.match(fix_id):
        raise LocationError("malformed", f"{field}.id is malformed")
    source = value.get("source")
    if source not in SOURCES:
        raise LocationError("malformed", f"{field}.source must be one of {', '.join(SOURCES)}")
    layer = "talking" if source in TALKING_SOURCES else "background"
    if not sharing[layer]:
        raise LocationError("layer_off", f"A {source} fix needs {layer} sharing on", status=409)
    at = parse_time(value.get("at"), f"{field}.at")
    place_id = value.get("place_id")
    if place_id is not None and (not isinstance(place_id, str) or place_id not in places):
        raise LocationError("malformed", f"{field}.place_id isn't one of the saved places")
    lat = _number(value.get("lat"), f"{field}.lat", -90, 90)
    lon = _number(value.get("lon"), f"{field}.lon", -180, 180)
    if (lat is None) != (lon is None):
        raise LocationError("malformed", f"{field} needs both lat and lon, or neither")
    accuracy = _number(value.get("accuracy_m"), f"{field}.accuracy_m", 0, 1_000_000)
    speed = _number(value.get("speed_mps"), f"{field}.speed_mps", -1, 500)
    raw_distances = value.get("distances_m")
    distances: dict[str, float] = {}
    if raw_distances is not None:
        if not isinstance(raw_distances, dict) or len(raw_distances) > MAX_PLACES:
            raise LocationError("malformed", f"{field}.distances_m must be an object of saved places")
        for key, metres in raw_distances.items():
            if key not in places:
                raise LocationError("malformed", f"{field}.distances_m names an unknown place")
            metres = _number(metres, f"{field}.distances_m.{key}", 0, 50_000_000)
            if metres is not None:  # null: the phone couldn't say, not "here"
                distances[key] = metres
    area = _parse_area(value.get("area"), f"{field}.area")
    # The user's chosen detail, enforced here as well as on the phone.
    precision = sharing[f"{layer}_precision"]
    private = place_id is not None and places[place_id]["private"]
    if precision == "places" or private:
        lat = lon = accuracy = speed = None
        area = None
        distances = {}
    elif precision == "area":
        if lat is not None:
            lat, lon = round(lat, 2), round(lon, 2)
        accuracy = max(accuracy or 0.0, 1000.0) if lat is not None else None
        distances = {key: round(metres / 100) * 100 for key, metres in distances.items()}
        speed = None
    else:
        distances = {key: round(metres / 10) * 10 for key, metres in distances.items()}
    return {"id": fix_id, "at": at.timestamp(), "at_local": at.isoformat(), "source": source,
            "precision": "places" if private else precision, "lat": lat, "lon": lon, "accuracy": accuracy,
            "place_id": place_id, "area": area, "distances": distances, "speed": speed}


def _parse_event(value: Any, index: int, places: dict[str, dict[str, Any]], sharing: dict[str, Any]) -> dict[str, Any]:
    field = f"events[{index}]"
    if not isinstance(value, dict):
        raise LocationError("malformed", f"{field} must be an object")
    event_id = value.get("id")
    if not isinstance(event_id, str) or not _RECORD_ID.match(event_id):
        raise LocationError("malformed", f"{field}.id is malformed")
    if value.get("kind") not in EVENT_KINDS:
        raise LocationError("malformed", f"{field}.kind must be arrive or leave")
    place_id = value.get("place_id")
    if not isinstance(place_id, str) or place_id not in places:
        raise LocationError("malformed", f"{field}.place_id isn't one of the saved places")
    at = parse_time(value.get("at"), f"{field}.at")
    private = places[place_id]["private"]
    area = None if private or sharing["background_precision"] == "places" else _parse_area(
        value.get("area"), f"{field}.area")
    return {"id": event_id, "kind": value["kind"], "place_id": place_id, "at": at.timestamp(), "local": at,
            "private": private, "area": area}


def _decode(text: str | None, default: Any) -> Any:
    """Stored JSON, or ``default`` when it's missing or damaged."""
    if not text:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


def _area_text(area: dict[str, str] | None) -> str | None:
    if not area:
        return None
    parts = [area.get(key) for key in ("name", "locality", "country")]
    seen: list[str] = []
    for part in parts:
        if part and part not in seen:
            seen.append(part)
    return ", ".join(seen) or None


def _forget_newly_private(db: sqlite3.Connection, device_id: str, places_list: list[dict[str, Any]]) -> None:
    """A place the user just made private: what was kept while it wasn't (a
    fix there with its coordinates, arrivals and departures naming it) goes,
    as if it had been private all along."""
    was_public = {place_id for place_id, private in db.execute(
        "SELECT id, private FROM places WHERE device=?", (device_id,)) if not private}
    for place in places_list:
        if place["private"] and place["id"] in was_public:
            db.execute("DELETE FROM visits WHERE device=? AND place_id=?", (device_id, place["id"]))
            db.execute("UPDATE fixes SET precision='places', lat=NULL, lon=NULL, accuracy=NULL, area=NULL,"
                       " distances=NULL, speed=NULL WHERE device=? AND place_id=?", (device_id, place["id"]))


def _apply(db: sqlite3.Connection, device_id: str, batch: dict[str, Any], now: float) -> dict[str, Any]:
    # A phone clock running ahead mustn't make the batches after it look old.
    sent = min(parse_time(batch.get("sent_at"), "sent_at").timestamp(), now)
    sharing = _parse_sharing(batch.get("sharing"))
    if batch.get("places") is not None:
        places_list = _parse_places(batch.get("places"))
    else:
        places_list = None
    if places_list is None:
        current = {place_id: {"id": place_id, "name": name, "kind": kind, "private": bool(private)}
                   for place_id, name, kind, private in db.execute(
                       "SELECT id, name, kind, private FROM places WHERE device=?", (device_id,))}
    else:
        current = {place["id"]: place for place in places_list}
    paused = sharing_paused(sharing, now)
    events_raw = _list(batch.get("events"), "events", MAX_EVENTS)
    # Validate everything before writing anything.
    events = [_parse_event(event, index, current, sharing) for index, event in enumerate(events_raw)]
    if events and not sharing["background"]:
        raise LocationError("layer_off", "Arrivals and departures need background sharing on", status=409)
    fix = None if batch.get("fix") is None or paused else _parse_fix(batch.get("fix"), sharing, current)
    if fix is not None and fix["at"] > now + RULE_EVENT_SKEW_S:
        # A phone clock running ahead: don't let a fix from "the future"
        # hide every real one after it.
        fix["at"] = now
        fix["at_local"] = datetime.fromtimestamp(now, datetime.fromisoformat(fix["at_local"]).tzinfo).isoformat()
    locate_id = batch.get("locate_id")
    if locate_id is not None and (not isinstance(locate_id, str) or not _LOCATE_ID.match(locate_id)):
        raise LocationError("malformed", "locate_id is malformed")

    last_sent = db.execute("SELECT last_sent FROM devices WHERE id=?", (device_id,)).fetchone()[0]
    if last_sent is not None and sent < last_sent:
        # An older batch (a retry that arrived late, or a replay) never
        # undoes a newer one: not its sharing or pause, places, fix or moves.
        db.execute("UPDATE devices SET last_seen=? WHERE id=?", (now, device_id))
        return {"accepted": True, "stale": True, "fix": False, "events": 0, "rules_fired": []}
    db.execute("UPDATE devices SET last_seen=?, last_sent=?, sharing=? WHERE id=?",
               (now, sent, json.dumps(sharing), device_id))
    if places_list is not None:
        _forget_newly_private(db, device_id, places_list)
        db.execute("DELETE FROM places WHERE device=?", (device_id,))
        for position, place in enumerate(places_list):
            db.execute("INSERT INTO places VALUES(?,?,?,?,?,?)",
                       (device_id, place["id"], position, place["name"], place["kind"], 1 if place["private"] else 0))
    if paused:
        # Nothing stays from before the pause: the agent hears "paused", not
        # where the user last was.
        db.execute("DELETE FROM fixes WHERE device=?", (device_id,))
    stored_fix = False
    if fix is not None:
        newer = db.execute("SELECT 1 FROM fixes WHERE device=? AND at>?", (device_id, fix["at"])).fetchone()
        if not newer:
            db.execute(
                "INSERT OR REPLACE INTO fixes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (device_id, fix["id"], fix["at"], fix["at_local"], fix["source"], fix["precision"], fix["lat"],
                 fix["lon"], fix["accuracy"], fix["place_id"], json.dumps(fix["area"]) if fix["area"] else None,
                 json.dumps(fix["distances"]) if fix["distances"] else None, fix["speed"],
                 locate_id if fix["source"] == "locate" else None, now))
            stored_fix = True
    if locate_id is not None and fix is not None and fix["source"] == "locate":
        db.execute("UPDATE locates SET answered_at=? WHERE id=? AND answered_at IS NULL", (now, locate_id))

    fired: list[dict[str, Any]] = []
    added = 0
    for event in events:
        if event["at"] > now + RULE_EVENT_SKEW_S:
            continue  # stamped ahead of this host's clock: not history yet
        place = current[event["place_id"]]
        if not paused and not event["private"] and sharing["retention_days"] > 0:
            cursor = db.execute(
                "INSERT OR IGNORE INTO visits VALUES(?,?,?,?,?,?,?,?,?)",
                (device_id, event["id"], event["kind"], place["id"], place["name"], _area_text(event["area"]),
                 event["at"], event["local"].isoformat(), now))
            is_new = bool(cursor.rowcount)
            added += 1 if is_new else 0
        else:
            # Kept nowhere (paused, private or no history), but a replayed
            # batch still mustn't run a rule twice: rules fire at most once a
            # day anyway.
            is_new = True
        # Not for an old move, nor one stamped in the future (beyond clock
        # skew): it would run now and use up the day it names.
        if is_new and not paused and -RULE_EVENT_SKEW_S <= now - event["at"] <= RULE_EVENT_MAX_AGE_S:
            fired.extend(_fire_rules(db, event, place, now))
    _prune(db, device_id, sharing["retention_days"], now)
    return {"accepted": True, "fix": stored_fix, "events": added, "rules_fired": fired}


def _fire_rules(db: sqlite3.Connection, event: dict[str, Any], place: dict[str, Any], now: float) -> list[dict[str, Any]]:
    local: datetime = event["local"]
    day = local.date().isoformat()
    weekday = WEEKDAYS[local.weekday()]
    minutes = local.hour * 60 + local.minute
    fired = []
    for row in db.execute(f"SELECT {_RULE_COLUMNS} FROM rules WHERE place_id IN (?, ?) AND event IN (?, 'either')",
                          (place["id"], ANY_PLACE, event["kind"])).fetchall():
        rule = _rule_row(row)
        if rule["days"] and weekday not in rule["days"]:
            continue
        if not in_hours(minutes, parse_hours(rule["hours"]) if rule["hours"] else None):
            continue
        if rule["repeat"] == "daily" and rule["last_fired"] == day:
            continue
        if rule["repeat"] == "every" and rule["last_fired_ts"] and now - rule["last_fired_ts"] < RULE_EVERY_MIN_GAP_S:
            continue
        db.execute("UPDATE rules SET last_fired_day=?, last_fired_at=? WHERE id=?", (day, now, rule["id"]))
        verb = "arrived at" if event["kind"] == "arrive" else "left"
        where = "a private place" if place["private"] else place["name"]
        fired.append({"rule_id": rule["id"], "job_id": rule["job_id"], "what": rule["what"],
                      "prompt": f"[Conduit place event: the user {verb} {where} at "
                                f"{local.hour:02d}:{local.minute:02d} ({_DAY_NAMES[local.weekday()]} {local.day:02d} "
                                f"{_MONTH_NAMES[local.month - 1]}, their local time).]"})
    return fired


# Spelled out so the event line reads the same whatever the host's locale.
_DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _prune(db: sqlite3.Connection, device_id: str, retention_days: int, now: float) -> None:
    if retention_days <= 0:
        db.execute("DELETE FROM visits WHERE device=?", (device_id,))
    else:
        db.execute("DELETE FROM visits WHERE device=? AND at<?", (device_id, now - retention_days * 86400))
    db.execute("DELETE FROM reads WHERE at<?", (now - READS_KEPT_S,))
    db.execute("DELETE FROM locates WHERE requested_at<?", (now - LOCATES_KEPT_S,))
