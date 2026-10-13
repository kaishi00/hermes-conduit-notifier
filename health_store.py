"""Health data from Conduit: the store, its encryption and its queries.

Conduit can share Apple Health data with this profile so Hermes can read it
(off until the user turns it on, per category). The phone sends it straight
to this host over the dashboard connection, never through the push relay.

What arrives:

* buckets: hourly and daily totals or min/avg/max that HealthKit itself
  computes on the phone (it merges iPhone and Watch by the user's source
  priority, which summing raw samples here could not), plus one summary per
  night of sleep. The phone recomputes recent days on every sync and sends
  each metric's window whole, so a window replaces what was stored for it.
* events: things with their own HealthKit id: workouts, sleep stage
  segments, and raw readings when the user turns on detailed readings.
  Deleted ids become tombstones, so a late re-send can't bring them back.

Every ingest body is sealed end to end, on top of whatever TLS the dashboard
has, so a host reached over plain http on a LAN still never carries health
data in the clear. Each device agrees a key with this profile by X25519
(``/health/key``): only public keys cross the wire, so someone who watched
that exchange still can't open later bodies. HKDF-SHA256 and
ChaCha20-Poly1305 as in e2e.py; the associated data binds a body to its key
and batch.

State lives in the profile's ``conduit-health.sqlite3`` (owner-only). The
dashboard route (dashboard process) writes it and the ``conduit_health`` tool
(agent process) reads it: the dashboard loads this file by path, so it
imports nothing from the plugin package.
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

FILE_NAME = "conduit-health.sqlite3"
SCHEMA_VERSION = 1

SALT = b"conduit-health-v1"
INFO_INGEST = b"conduit-health-v1 ingest device-to-host"
AAD_TAG = "conduit-health/1"
NONCE_BYTES = 12
KEY_BYTES = 32

# What Conduit may send, each with its one unit. A metric not listed is
# refused, so the store never fills with names the tool can't explain.
# "sum": cumulative (sum per bucket); "stats": discrete (min/avg/max).
METRICS: dict[str, tuple[str, str, str]] = {
    # metric: (category, unit, aggregation)
    "steps": ("activity", "count", "sum"),
    "distance_walking_running": ("activity", "m", "sum"),
    "distance_cycling": ("activity", "m", "sum"),
    "active_energy": ("activity", "kcal", "sum"),
    "exercise_time": ("activity", "min", "sum"),
    "stand_time": ("activity", "min", "sum"),
    "flights_climbed": ("activity", "count", "sum"),
    "heart_rate": ("heart", "count/min", "stats"),
    "resting_heart_rate": ("heart", "count/min", "stats"),
    "walking_heart_rate_average": ("heart", "count/min", "stats"),
    "heart_rate_variability": ("heart", "ms", "stats"),
    "oxygen_saturation": ("vitals", "%", "stats"),
    "respiratory_rate": ("vitals", "count/min", "stats"),
    "blood_pressure_systolic": ("vitals", "mmHg", "stats"),
    "blood_pressure_diastolic": ("vitals", "mmHg", "stats"),
    "wrist_temperature": ("vitals", "degC", "stats"),
    "body_mass": ("body", "kg", "stats"),
    "body_fat_percentage": ("body", "%", "stats"),
    "height": ("body", "m", "stats"),
    "vo2_max": ("fitness", "mL/(kg*min)", "stats"),
    "walking_speed": ("fitness", "m/s", "stats"),
    "six_minute_walk_distance": ("fitness", "m", "stats"),
    "sleep": ("sleep", "min", "night"),
    "workout": ("workouts", "", "event"),
}
CATEGORIES = ("activity", "heart", "sleep", "workouts", "body", "vitals", "fitness")
GRAINS = ("hour", "day", "night")
EVENT_KINDS = ("workout", "sleep", "sample")
SLEEP_STAGES = ("in_bed", "asleep", "core", "deep", "rem", "awake")
NIGHT_FIELDS = ("asleep", "in_bed", "core", "deep", "rem", "awake", "unspecified")

MAX_DEVICES = 4
MAX_ITEMS = 6000  # buckets + events + deletions in one batch
MAX_WINDOWS = 200
MAX_EVENT_DATA_KEYS = 16
MAX_TEXT_CHARS = 120
MAX_DEVICE_NAME_CHARS = 60
# A window may cover at most this much (backfill sends a year of daily
# buckets for a metric in one window).
MAX_WINDOW_DAYS = 400
EARLIEST = datetime(2000, 1, 1, tzinfo=timezone.utc)

_DEVICE_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}\Z")
_EVENT_ID = re.compile(r"^[A-Za-z0-9:_-]{1,80}\Z")
_BATCH_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}\Z")
_KID = re.compile(r"^[0-9a-f]{32}\Z")
_B64URL = re.compile(r"^[A-Za-z0-9_-]*\Z")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}\Z")
_TZ = re.compile(r"^[A-Za-z0-9_+\-/]{1,64}\Z")

# Line breaks, other control characters and invisible format characters
# (bidi overrides, zero-width), which could make a device name or source
# read like instructions in the agent's tool result.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2060-\u206f\ufeff]+")

_thread_lock = threading.Lock()


class HealthError(ValueError):
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
        raise HealthError("malformed", "malformed base64url")
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
    return hashlib.sha256(b"conduit-health-kid\n" + device_public + host_public).hexdigest()[:32]


def derive_key(shared: bytes, device_id: str, device_public: bytes, host_public: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    info = INFO_INGEST + b"\n" + device_id.encode("ascii") + b"\n" + device_public + host_public
    return HKDF(algorithm=hashes.SHA256(), length=KEY_BYTES, salt=SALT, info=info).derive(shared)


def agree(host_private: bytes, device_public: bytes) -> bytes:
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey

    try:
        shared = X25519PrivateKey.from_private_bytes(host_private).exchange(
            X25519PublicKey.from_public_bytes(device_public))
    except ValueError:  # OpenSSL refuses an all-zero result itself
        shared = bytes(32)
    if shared == bytes(32):
        raise HealthError("bad_key", "That public key can't be used")
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
        raise HealthError("malformed", "nonce must be 12 bytes")
    raw_ciphertext = unb64u(ciphertext)
    if len(raw_ciphertext) < 16:
        raise HealthError("malformed", "ct is shorter than its tag")
    try:
        return ChaCha20Poly1305(key).decrypt(raw_nonce, raw_ciphertext, aad)
    except InvalidTag:
        raise HealthError("bad_seal", "The body didn't open with this device's key; set up health sync again",
                          status=409) from None


# --- Time helpers --------------------------------------------------------------


def _parse_time(value: Any, field: str) -> datetime:
    """An ISO 8601 time with an offset (the phone's local offset is kept so
    "the 13th" means the user's 13th)."""
    if not isinstance(value, str) or len(value) > 40:
        raise HealthError("malformed", f"{field} must be an ISO 8601 time")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HealthError("malformed", f"{field} must be an ISO 8601 time") from None
    if parsed.tzinfo is None:
        raise HealthError("malformed", f"{field} needs a UTC offset")
    if parsed < EARLIEST or parsed > datetime.now(timezone.utc) + timedelta(days=2):
        raise HealthError("malformed", f"{field} is out of range")
    return parsed


def _epoch(value: datetime) -> int:
    return int(value.timestamp())


def _iso_utc(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _number(value: Any, field: str, *, allow_none: bool = True) -> float | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise HealthError("malformed", f"{field} must be a finite number")
    if abs(value) > 1e9:
        raise HealthError("malformed", f"{field} is out of range")
    return float(value)


def _text(value: Any, field: str, limit: int = MAX_TEXT_CHARS) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise HealthError("malformed", f"{field} must be text of at most {limit} characters")
    return _CONTROL.sub(" ", value).strip() or None


def _flat_data(value: Any, field: str) -> dict[str, Any]:
    """A small flat object of numbers and short text, nothing nested."""
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > MAX_EVENT_DATA_KEYS:
        raise HealthError("malformed", f"{field} must be an object with at most {MAX_EVENT_DATA_KEYS} keys")
    clean: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", key):
            raise HealthError("malformed", f"{field} keys must be short snake_case names")
        if isinstance(item, str):
            text = _text(item, f"{field}.{key}")
            if text is not None:
                clean[key] = text
        elif item is None:
            continue
        else:
            clean[key] = _number(item, f"{field}.{key}", allow_none=False)
    return clean


# --- Store -----------------------------------------------------------------------


class HealthStore:
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
                connection.execute("PRAGMA foreign_keys=ON")
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
        out. Replaces the device's earlier key (the phone re-keys after a
        reinstall or when the user turns sync off and on)."""
        if not isinstance(body, dict):
            raise HealthError("malformed", "Expected a JSON object")
        device_id = body.get("device_id")
        if not isinstance(device_id, str) or not _DEVICE_ID.match(device_id):
            raise HealthError("malformed", "device_id must be 8-64 letters, digits, - or _")
        name = _text(body.get("device_name"), "device_name", MAX_DEVICE_NAME_CHARS) or "iPhone"
        device_public = unb64u(body.get("public_key"))
        if len(device_public) != 32:
            raise HealthError("malformed", "public_key must be a 32-byte X25519 key")
        if not crypto_available():
            raise HealthError("no_crypto", "Health sync needs the cryptography package on this host", status=501)
        host_private, host_public = new_host_keypair()
        agree(host_private, device_public)  # refuses a low-order key now, not at the first ingest
        kid = key_id(device_public, host_public)
        now = int(self.clock())
        with self._write() as db:
            known = db.execute("SELECT 1 FROM devices WHERE id=?", (device_id,)).fetchone()
            count = db.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
            if not known and count >= MAX_DEVICES:
                raise HealthError("too_many_devices",
                                  f"This profile already keeps health data from {MAX_DEVICES} devices; delete one "
                                  "from Conduit first", status=409)
            db.execute(
                "INSERT INTO devices(id, name, device_public, host_private, host_public, kid, created_at, last_seen)"
                " VALUES(?,?,?,?,?,?,?,NULL)"
                " ON CONFLICT(id) DO UPDATE SET name=excluded.name, device_public=excluded.device_public,"
                " host_private=excluded.host_private, host_public=excluded.host_public, kid=excluded.kid,"
                " created_at=excluded.created_at",
                (device_id, name, device_public, host_private, host_public, kid, now),
            )
            generation = _meta(db, "generation")
        return {"kid": kid, "host_public_key": b64u(host_public), "generation": generation}

    def _device_key(self, db: sqlite3.Connection, device_id: str, kid: str) -> bytes:
        row = db.execute("SELECT device_public, host_private, host_public, kid FROM devices WHERE id=?",
                         (device_id,)).fetchone()
        if row is None or row[3] != kid:
            raise HealthError("unknown_key", "This device's health key isn't on this profile; set it up again",
                              status=409)
        device_public, host_private, host_public = bytes(row[0]), bytes(row[1]), bytes(row[2])
        return derive_key(agree(host_private, device_public), device_id, device_public, host_public)

    # Ingest --------------------------------------------------------------------

    def ingest(self, envelope: Any) -> dict[str, Any]:
        if not isinstance(envelope, dict) or type(envelope.get("v")) is not int or envelope.get("v") != 1:
            raise HealthError("malformed", "Expected a version 1 sealed batch")
        device_id, kid, batch_id = envelope.get("device_id"), envelope.get("kid"), envelope.get("batch_id")
        if not isinstance(device_id, str) or not _DEVICE_ID.match(device_id):
            raise HealthError("malformed", "device_id is malformed")
        if not isinstance(kid, str) or not _KID.match(kid):
            raise HealthError("malformed", "kid is malformed")
        if not isinstance(batch_id, str) or not _BATCH_ID.match(batch_id):
            raise HealthError("malformed", "batch_id is malformed")
        if not crypto_available():
            raise HealthError("no_crypto", "Health sync needs the cryptography package on this host", status=501)
        now = int(self.clock())
        with self._write() as db:
            key = self._device_key(db, device_id, kid)
            plaintext = open_sealed(key, envelope.get("nonce"), envelope.get("ct"), ingest_aad(kid, device_id, batch_id))
            try:
                batch = json.loads(plaintext)
            except (ValueError, RecursionError):
                raise HealthError("malformed", "The sealed batch isn't JSON") from None
            if not isinstance(batch, dict) or batch.get("batch_id") != batch_id:
                raise HealthError("malformed", "The sealed batch doesn't match its envelope")
            result = _apply(db, device_id, batch, now)
            generation = _meta(db, "generation")
        return {**result, "generation": generation}

    # Status and wipe ---------------------------------------------------------------

    def status(self, device_id: Any = None) -> dict[str, Any]:
        with self._read() as db:
            if db is None:
                return {"stored": False, "generation": None, "device": None}
            answer: dict[str, Any] = {"stored": True, "generation": _meta(db, "generation"), "device": None}
            if isinstance(device_id, str) and _DEVICE_ID.match(device_id):
                row = db.execute("SELECT kid, last_seen, categories FROM devices WHERE id=?", (device_id,)).fetchone()
                if row is not None:
                    counts = {metric: count for metric, count in db.execute(
                        "SELECT metric, COUNT(*) FROM buckets WHERE device=? GROUP BY metric", (device_id,))}
                    for metric, count in db.execute(
                            "SELECT metric, COUNT(*) FROM events WHERE device=? GROUP BY metric", (device_id,)):
                        counts[metric] = counts.get(metric, 0) + count
                    answer["device"] = {"kid": row[0], "last_sync": _iso_utc(row[1]) if row[1] else None,
                                        "categories": json.loads(row[2] or "[]"), "rows": counts}
            return answer

    def wipe(self, device_id: Any = None) -> dict[str, Any]:
        """Deletes one device's health data and key, or everything when
        ``device_id`` is None. The file goes once no device is left."""
        if device_id is not None and (not isinstance(device_id, str) or not _DEVICE_ID.match(device_id)):
            raise HealthError("malformed", "device_id is malformed")
        if not self.path.exists():
            return {"deleted": True, "remaining_devices": 0}
        with self._write() as db:
            for table in ("buckets", "events", "tombstones", "windows", "devices"):
                column = "id" if table == "devices" else "device"
                if device_id is None:
                    db.execute(f"DELETE FROM {table}")
                else:
                    db.execute(f"DELETE FROM {table} WHERE {column}=?", (device_id,))
        # The file goes once no device is left. Counted again under the
        # writers' lock, so a key registered since the delete stays.
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

    # Queries (the agent's tool) ------------------------------------------------------

    def overview(self) -> dict[str, Any]:
        with self._read() as db:
            if db is None:
                return {"devices": []}
            devices = []
            for device_id, name, last_seen, categories, tz, detailed in db.execute(
                    "SELECT id, name, last_seen, categories, tz, detailed FROM devices"
                    " WHERE last_seen IS NOT NULL ORDER BY last_seen DESC"):
                metrics = {}
                for metric, grain, first, last in db.execute(
                        "SELECT metric, grain, MIN(date), MAX(date) FROM buckets WHERE device=?"
                        " GROUP BY metric, grain", (device_id,)):
                    entry = metrics.setdefault(metric, {"unit": METRICS.get(metric, ("", "", ""))[1], "grains": []})
                    entry["grains"].append(grain)
                    entry["first"] = min(entry.get("first", first), first)
                    entry["last"] = max(entry.get("last", last), last)
                for metric, kind, count, first, last in db.execute(
                        "SELECT metric, kind, COUNT(*), MIN(start_local), MAX(start_local) FROM events WHERE device=?"
                        " GROUP BY metric, kind", (device_id,)):
                    entry = metrics.setdefault(metric, {"unit": METRICS.get(metric, ("", "", ""))[1], "grains": []})
                    entry.setdefault("events", {})[kind] = count
                    entry["first"] = min(entry.get("first", first[:10]), first[:10])
                    entry["last"] = max(entry.get("last", last[:10]), last[:10])
                devices.append({"id": device_id, "name": name, "last_sync": _iso_utc(last_seen),
                                "time_zone": tz, "categories": json.loads(categories or "[]"),
                                "detailed_readings": json.loads(detailed or "[]"), "metrics": metrics})
            return {"devices": devices}

    def latest_device(self, db: sqlite3.Connection, wanted: Any = None) -> tuple[str, str, int] | None:
        rows = db.execute("SELECT id, name, last_seen FROM devices WHERE last_seen IS NOT NULL"
                          " ORDER BY last_seen DESC").fetchall()
        if isinstance(wanted, str) and wanted:
            rows = [row for row in rows if row[0] == wanted] or [
                row for row in rows if row[1].casefold() == wanted.casefold()]
            if len(rows) != 1:
                raise HealthError("unknown_device", "No single device has that id or name; use one from overview")
        return (rows[0][0], rows[0][1], rows[0][2]) if rows else None

    @contextlib.contextmanager
    def reader(self, device: Any = None) -> Iterator[tuple[sqlite3.Connection, tuple[str, str, int]] | None]:
        with self._read() as db:
            if db is None:
                yield None
                return
            chosen = self.latest_device(db, device)
            yield (db, chosen) if chosen else None

    def daily(self, db: sqlite3.Connection, device: str, metrics: list[str], start: str, end: str) -> list[dict]:
        rows: dict[str, dict[str, Any]] = {}
        placeholders = ",".join("?" * len(metrics))
        for metric, day, total, low, mean, high, count in db.execute(
                f"SELECT metric, date, sum, min, avg, max, count FROM buckets WHERE device=? AND grain='day'"
                f" AND metric IN ({placeholders}) AND date BETWEEN ? AND ? ORDER BY date",
                (device, *metrics, start, end)):
            rows.setdefault(day, {"date": day})[metric] = _value(metric, total, low, mean, high, count)
        return [rows[day] for day in sorted(rows)]

    def hourly(self, db: sqlite3.Connection, device: str, metrics: list[str], day: str) -> list[dict]:
        rows: dict[str, dict[str, Any]] = {}
        placeholders = ",".join("?" * len(metrics))
        for metric, start, total, low, mean, high, count in db.execute(
                f"SELECT metric, start_local, sum, min, avg, max, count FROM buckets WHERE device=?"
                f" AND grain='hour' AND metric IN ({placeholders}) AND date=? ORDER BY start_utc",
                (device, *metrics, day)):
            hour = start[11:16]
            rows.setdefault(hour, {"hour": hour})[metric] = _value(metric, total, low, mean, high, count)
        return [rows[hour] for hour in sorted(rows)]

    def nights(self, db: sqlite3.Connection, device: str, start: str, end: str) -> list[dict]:
        answer = []
        for day, data, source in db.execute(
                "SELECT date, data, source FROM buckets WHERE device=? AND grain='night' AND metric='sleep'"
                " AND date BETWEEN ? AND ? ORDER BY date", (device, start, end)):
            night = {"night_ending": day, **json.loads(data or "{}")}
            if source:
                night["source"] = source
            answer.append(night)
        return answer

    def events(self, db: sqlite3.Connection, device: str, kind: str, metric: str | None, start: str, end: str,
               limit: int, activity: str | None = None) -> tuple[list[dict], bool]:
        query = ("SELECT id, metric, start_local, end_local, value, unit, data, source FROM events"
                 " WHERE device=? AND kind=? AND substr(start_local,1,10) BETWEEN ? AND ?")
        args: list[Any] = [device, kind, start, end]
        if metric:
            query += " AND metric=?"
            args.append(metric)
        if activity:
            # Filtered before the limit, so a busy range still finds them.
            query += " AND lower(json_extract(data, '$.activity'))=?"
            args.append(activity.lower())
        query += " ORDER BY start_utc LIMIT ?"
        args.append(limit + 1)
        found = []
        for event_id, event_metric, start_local, end_local, value, unit, data, source in db.execute(query, args):
            entry: dict[str, Any] = {"start": start_local, "end": end_local}
            if kind == "sample":
                entry["metric"] = event_metric
            if value is not None:
                entry["value"] = _round(value)
                entry["unit"] = unit
            # A workout's own details, never in place of the fixed fields.
            for key, item in json.loads(data or "{}").items():
                if key not in _EVENT_FIELDS:
                    entry[key] = item
            if source:
                entry["source"] = source
            found.append(entry)
        return found[:limit], len(found) > limit


def _value(metric: str, total: Any, low: Any, mean: Any, high: Any, count: Any) -> Any:
    aggregation = METRICS.get(metric, ("", "", "stats"))[2]
    if aggregation == "sum":
        return _round(total)
    shown = {"avg": _round(mean), "min": _round(low), "max": _round(high)}
    return {key: value for key, value in shown.items() if value is not None}


def _round(value: Any) -> Any:
    if value is None:
        return None
    return round(value, 1) if abs(value) >= 10 else round(value, 2)


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
        raise HealthError("schema", "This health store was written by a newer plugin; update the plugin", status=409)
    # One statement at a time: executescript would commit the open transaction.
    for statement in """
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS devices(
            id TEXT PRIMARY KEY, name TEXT NOT NULL,
            device_public BLOB NOT NULL, host_private BLOB NOT NULL, host_public BLOB NOT NULL, kid TEXT NOT NULL,
            created_at INTEGER NOT NULL, last_seen INTEGER, tz TEXT, categories TEXT, detailed TEXT);
        CREATE TABLE IF NOT EXISTS buckets(
            device TEXT NOT NULL, metric TEXT NOT NULL, grain TEXT NOT NULL,
            start_utc INTEGER NOT NULL, end_utc INTEGER NOT NULL, start_local TEXT NOT NULL, date TEXT NOT NULL,
            sum REAL, min REAL, avg REAL, max REAL, count INTEGER, unit TEXT NOT NULL, data TEXT, source TEXT,
            computed_at INTEGER NOT NULL,
            PRIMARY KEY(device, metric, grain, start_utc));
        CREATE INDEX IF NOT EXISTS buckets_by_date ON buckets(device, grain, metric, date);
        CREATE TABLE IF NOT EXISTS windows(
            device TEXT NOT NULL, metric TEXT NOT NULL, grain TEXT NOT NULL,
            start_utc INTEGER NOT NULL, end_utc INTEGER NOT NULL, computed_at INTEGER NOT NULL,
            PRIMARY KEY(device, metric, grain, start_utc, end_utc));
        CREATE TABLE IF NOT EXISTS events(
            device TEXT NOT NULL, id TEXT NOT NULL, kind TEXT NOT NULL, metric TEXT NOT NULL,
            start_utc INTEGER NOT NULL, end_utc INTEGER NOT NULL, start_local TEXT NOT NULL, end_local TEXT NOT NULL,
            value REAL, unit TEXT, data TEXT, source TEXT, received_at INTEGER NOT NULL,
            PRIMARY KEY(device, id));
        CREATE INDEX IF NOT EXISTS events_by_time ON events(device, kind, metric, start_utc);
        CREATE TABLE IF NOT EXISTS tombstones(
            device TEXT NOT NULL, id TEXT NOT NULL, deleted_at INTEGER NOT NULL, PRIMARY KEY(device, id));
    """.split(";"):
        if statement.strip():
            db.execute(statement)
    db.execute("INSERT OR REPLACE INTO meta VALUES('schema', ?)", (str(SCHEMA_VERSION),))
    db.execute("INSERT OR IGNORE INTO meta VALUES('generation', ?)", (secrets.token_hex(8),))
    db.execute("INSERT OR IGNORE INTO meta VALUES('created_at', ?)", (str(int(now)),))


_EVENT_FIELDS = frozenset({"id", "start", "end", "metric", "value", "unit", "source"})


def _real_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _list_or_empty(container: dict[str, Any], field: str) -> Any:
    """The field, or an empty list when it's missing or null. Anything else
    goes to the caller's list check as it is (0 and "" are not lists)."""
    value = container.get(field)
    return [] if value is None else value


def _apply(db: sqlite3.Connection, device_id: str, batch: dict[str, Any], now: int) -> dict[str, Any]:
    windows, events, deleted = (_list_or_empty(batch, field) for field in ("windows", "events", "deleted"))
    if not isinstance(windows, list) or not isinstance(events, list) or not isinstance(deleted, list):
        raise HealthError("malformed", "windows, events and deleted must be lists")
    if len(windows) > MAX_WINDOWS:
        raise HealthError("too_large", f"At most {MAX_WINDOWS} windows per batch", status=413)
    total = len(events) + len(deleted) + sum(
        len(window["buckets"]) for window in windows
        if isinstance(window, dict) and isinstance(window.get("buckets"), list))
    if total > MAX_ITEMS:
        raise HealthError("too_large", f"At most {MAX_ITEMS} items per batch", status=413)
    computed_at = _epoch(_parse_time(batch.get("computed_at"), "computed_at"))
    device = batch.get("device")
    if device is None:
        device = {}
    elif not isinstance(device, dict):
        raise HealthError("malformed", "device must be an object")
    tz = device.get("time_zone")
    if tz is not None and (not isinstance(tz, str) or not _TZ.match(tz)):
        raise HealthError("malformed", "device.time_zone is malformed")
    for field in ("categories", "detailed"):
        if not isinstance(_list_or_empty(batch, field), list):
            raise HealthError("malformed", f"{field} must be a list")
    # Absent means "unchanged": a batch of only deletions keeps what the
    # device said it shares.
    categories = None if batch.get("categories") is None else [
        value for value in batch["categories"] if isinstance(value, str) and value in CATEGORIES]
    detailed = None if batch.get("detailed") is None else [
        value for value in batch["detailed"] if isinstance(value, str) and value in CATEGORIES]

    # Validate everything before writing anything, so a bad item can't leave
    # half a batch behind.
    parsed_windows = [_parse_window(window, index) for index, window in enumerate(windows)]
    parsed_events = [_parse_event(event, index) for index, event in enumerate(events)]
    for index, event_id in enumerate(deleted):
        if not isinstance(event_id, str) or not _EVENT_ID.match(event_id):
            raise HealthError("malformed", f"deleted[{index}] is malformed")

    replaced = skipped = 0
    for window in parsed_windows:
        newer = db.execute(
            "SELECT 1 FROM windows WHERE device=? AND metric=? AND grain=? AND start_utc<? AND end_utc>?"
            " AND computed_at>? LIMIT 1",
            (device_id, window["metric"], window["grain"], window["end"], window["start"], computed_at)).fetchone()
        if newer:
            # An older computation (a retried or replayed batch) never
            # overwrites a newer one.
            skipped += 1
            continue
        # Only this window's own range is replaced. An older window that
        # reaches past it keeps its tail until the phone sends that range
        # again, which it does for recent days on every pass.
        db.execute("DELETE FROM buckets WHERE device=? AND metric=? AND grain=? AND start_utc>=? AND start_utc<?",
                   (device_id, window["metric"], window["grain"], window["start"], window["end"]))
        db.execute("DELETE FROM windows WHERE device=? AND metric=? AND grain=? AND start_utc>=? AND end_utc<=?",
                   (device_id, window["metric"], window["grain"], window["start"], window["end"]))
        db.execute("INSERT OR REPLACE INTO windows VALUES(?,?,?,?,?,?)",
                   (device_id, window["metric"], window["grain"], window["start"], window["end"], computed_at))
        for bucket in window["buckets"]:
            db.execute(
                "INSERT OR REPLACE INTO buckets VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (device_id, window["metric"], window["grain"], bucket["start_utc"], bucket["end_utc"],
                 bucket["start_local"], bucket["date"], bucket.get("sum"), bucket.get("min"), bucket.get("avg"),
                 bucket.get("max"), bucket.get("count"), window["unit"], bucket.get("data"), bucket.get("source"),
                 computed_at))
        replaced += 1

    added = duplicates = 0
    for event in parsed_events:
        if db.execute("SELECT 1 FROM tombstones WHERE device=? AND id=?", (device_id, event["id"])).fetchone():
            duplicates += 1
            continue
        cursor = db.execute(
            "INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (device_id, event["id"], event["kind"], event["metric"], event["start_utc"], event["end_utc"],
             event["start_local"], event["end_local"], event.get("value"), event.get("unit"), event.get("data"),
             event.get("source"), now))
        if cursor.rowcount:
            added += 1
        else:
            duplicates += 1
    removed = 0
    for event_id in deleted:
        removed += db.execute("DELETE FROM events WHERE device=? AND id=?", (device_id, event_id)).rowcount
        db.execute("INSERT OR IGNORE INTO tombstones VALUES(?,?,?)", (device_id, event_id, now))

    db.execute("UPDATE devices SET last_seen=?, tz=COALESCE(?, tz), categories=COALESCE(?, categories),"
               " detailed=COALESCE(?, detailed) WHERE id=?",
               (now, tz, None if categories is None else json.dumps(categories),
                None if detailed is None else json.dumps(detailed), device_id))
    return {"windows": replaced, "stale_windows": skipped, "events": added, "duplicates": duplicates,
            "deleted": len(deleted), "removed": removed}


def _parse_window(window: Any, index: int) -> dict[str, Any]:
    field = f"windows[{index}]"
    if not isinstance(window, dict):
        raise HealthError("malformed", f"{field} must be an object")
    metric, grain = window.get("metric"), window.get("grain")
    if metric not in METRICS or METRICS[metric][2] == "event":
        raise HealthError("unknown_metric", f"{field}.metric isn't one this plugin keeps")
    category, unit, aggregation = METRICS[metric]
    if grain not in GRAINS or (grain == "night") != (aggregation == "night"):
        raise HealthError("malformed", f"{field}.grain doesn't fit {metric}")
    if window.get("unit") != unit:
        raise HealthError("bad_unit", f"{field}.unit must be {unit!r} for {metric}")
    start = _parse_time(window.get("from"), f"{field}.from")
    end = _parse_time(window.get("to"), f"{field}.to")
    if end <= start or end - start > timedelta(days=MAX_WINDOW_DAYS):
        raise HealthError("malformed", f"{field} must cover up to {MAX_WINDOW_DAYS} days, from before to")
    buckets = _list_or_empty(window, "buckets")
    if not isinstance(buckets, list):
        raise HealthError("malformed", f"{field}.buckets must be a list")
    parsed = []
    for position, bucket in enumerate(buckets):
        where = f"{field}.buckets[{position}]"
        if not isinstance(bucket, dict):
            raise HealthError("malformed", f"{where} must be an object")
        bucket_start = _parse_time(bucket.get("start"), f"{where}.start")
        bucket_end = _parse_time(bucket.get("end"), f"{where}.end")
        if bucket_start < start or bucket_start >= end or bucket_end <= bucket_start:
            raise HealthError("malformed", f"{where} must start inside its window")
        day = bucket.get("date")
        if grain != "night":
            day = bucket_start.date().isoformat()
        elif not isinstance(day, str) or not _DATE.match(day) or not _real_date(day):
            raise HealthError("malformed", f"{where}.date is the night's wake-up date, YYYY-MM-DD")
        entry: dict[str, Any] = {"start_utc": _epoch(bucket_start), "end_utc": _epoch(bucket_end),
                                 "start_local": bucket_start.isoformat(), "date": day,
                                 "source": _text(bucket.get("source"), f"{where}.source")}
        if aggregation == "sum":
            entry["sum"] = _number(bucket.get("sum"), f"{where}.sum", allow_none=False)
        elif aggregation == "stats":
            for key in ("min", "avg", "max"):
                entry[key] = _number(bucket.get(key), f"{where}.{key}")
            if entry["avg"] is None:
                raise HealthError("malformed", f"{where}.avg is required")
            count = bucket.get("count")
            if count is not None and (isinstance(count, bool) or not isinstance(count, int) or count < 0):
                raise HealthError("malformed", f"{where}.count must be a whole number")
            entry["count"] = count
        else:
            data = _flat_data(bucket.get("data"), f"{where}.data")
            unknown = set(data) - set(NIGHT_FIELDS) - {"wake_count", "bedtime", "wake_time"}
            if unknown:
                raise HealthError("malformed", f"{where}.data has unknown fields")
            # Minutes and counts are numbers; only the two times are text.
            if any(isinstance(data[key], str) for key in data if key not in ("bedtime", "wake_time")):
                raise HealthError("malformed", f"{where}.data minutes must be numbers")
            if any(not isinstance(data[key], str) for key in data if key in ("bedtime", "wake_time")):
                raise HealthError("malformed", f"{where}.data bedtime and wake_time must be times")
            entry["data"] = json.dumps(data, sort_keys=True)
        parsed.append(entry)
    return {"metric": metric, "grain": grain, "unit": unit, "category": category,
            "start": _epoch(start), "end": _epoch(end), "buckets": parsed}


def _parse_event(event: Any, index: int) -> dict[str, Any]:
    field = f"events[{index}]"
    if not isinstance(event, dict):
        raise HealthError("malformed", f"{field} must be an object")
    event_id, kind, metric = event.get("id"), event.get("kind"), event.get("metric")
    if not isinstance(event_id, str) or not _EVENT_ID.match(event_id):
        raise HealthError("malformed", f"{field}.id is malformed")
    if kind not in EVENT_KINDS:
        raise HealthError("malformed", f"{field}.kind must be one of {', '.join(EVENT_KINDS)}")
    expected = {"workout": "workout", "sleep": "sleep"}.get(kind)
    if expected and metric != expected:
        raise HealthError("malformed", f"{field}.metric must be {expected!r}")
    if kind == "sample" and (metric not in METRICS or METRICS[metric][2] not in ("sum", "stats")):
        raise HealthError("unknown_metric", f"{field}.metric isn't one this plugin keeps")
    start = _parse_time(event.get("start"), f"{field}.start")
    end = _parse_time(event.get("end"), f"{field}.end")
    if end < start or end - start > timedelta(days=7):
        raise HealthError("malformed", f"{field} must end after it starts, within a week")
    entry: dict[str, Any] = {"id": event_id, "kind": kind, "metric": metric, "start_utc": _epoch(start),
                             "end_utc": _epoch(end), "start_local": start.isoformat(), "end_local": end.isoformat(),
                             "source": _text(event.get("source"), f"{field}.source")}
    data = _flat_data(event.get("data"), f"{field}.data")
    if kind == "sample":
        entry["value"] = _number(event.get("value"), f"{field}.value", allow_none=False)
        unit = METRICS[metric][1]
        if event.get("unit") != unit:
            raise HealthError("bad_unit", f"{field}.unit must be {unit!r} for {metric}")
        entry["unit"] = unit
    elif kind == "sleep":
        if data.get("stage") not in SLEEP_STAGES:
            raise HealthError("malformed", f"{field}.data.stage must be one of {', '.join(SLEEP_STAGES)}")
    elif not isinstance(data.get("activity"), str):
        raise HealthError("malformed", f"{field}.data.activity names the workout")
    entry["data"] = json.dumps(data, sort_keys=True) if data else None
    return entry


def parse_day(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _DATE.match(value):
        raise HealthError("malformed", f"{field} must be a date, YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise HealthError("malformed", f"{field} must be a real date") from None
    return value
