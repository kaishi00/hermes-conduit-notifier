"""End-to-end encryption of Conduit notification content (#431).

Conduit creates a per-pairing root secret on the phone and hands it to this
profile through the dashboard connection (dashboard/plugin_api.py), so it
never passes through the push relay or APNs. From that secret both sides
derive separate keys with HKDF-SHA256:

* push:   gateway -> device notification content (ChaCha20-Poly1305)
* answer: device -> gateway clarify answers (ChaCha20-Poly1305)
* meta:   HMAC key for the thread token and re-keyed event ids

The associated data binds every envelope to its pairing (key id,
installation, gateway) and to the fields the relay can see (event id, type,
issue time, thread token, compression flag, clarify request id), so the
relay can neither read the content nor move, retype or re-thread a valid
ciphertext. Request ids and question ids bind clarify answers the same way.

Keys rotate every KEY_MAX_AGE_S, or when `hermes conduit-push e2e rotate`
asks for one. From plugin 0.72 the new secret is derived inline (a key
exchange sealed with the current key, see answer_rotation), so it never
crosses the dashboard connection; an app or host without inline rotation
hands a new secret over as before. Pushes are always sealed with the newest
key; the one it replaced stays as `e2e_previous` for PREVIOUS_KEY_GRACE_S,
to open clarify answers the phone sealed before it learned of the new one,
and (only after an inline rotation) to start the rotation over when the
phone never got its reply. That second use adds nothing over dashboard
access: a handover through POST /e2e can replace the key too.

Pure functions; the only dependency is `cryptography`, which Hermes already
pins as a core dependency. `available()` reports whether it can be used.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
import zlib
from dataclasses import dataclass
from typing import Any

# Same shape events.py checks; repeated so e2e stays import-free of events.
_LOCATE_ID = re.compile(r"^l_[A-Za-z0-9_-]{16,64}\Z")
_MAPS_ID = re.compile(r"^m_[A-Za-z0-9_-]{16,64}\Z")

VERSION = 1
SALT = b"conduit-e2e-v1"
INFO_PUSH = b"conduit-e2e-v1 push gateway-to-device"
INFO_ANSWER = b"conduit-e2e-v1 answer device-to-gateway"
INFO_META = b"conduit-e2e-v1 metadata"
AAD_TAG = "conduit-e2e/1"
ANSWER_PREFIX = "e2e1."
NONCE_BYTES = 12
SECRET_BYTES = 32
# The ciphertext has to fit an APNs payload (4 KB) next to the relay's alert,
# routing stub and envelope fields; the relay rejects anything longer.
MAX_CT_CHARS = 2600
# Fields that carry content. Everything else on an event stays visible to the
# relay because it routes on it.
INNER_FIELDS = ("title", "body", "session_id", "profile", "gateway", "stored_session_id", "decision", "call",
                "locate", "maps", "title_label", "session_title", "count")

# The phone rotates a key this old (inline, or by a handover) on its next
# dashboard connect.
KEY_MAX_AGE_S = 7 * 24 * 3600
# How long the key a rotation replaced still opens answers.
PREVIOUS_KEY_GRACE_S = 7 * 24 * 3600
KEY_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

KID_PATTERN = re.compile(r"^[0-9a-f]{32}\Z")
_B64URL_PATTERN = re.compile(r"^[A-Za-z0-9_-]*\Z")


class E2EError(Exception):
    """An envelope could not be built, opened or verified."""


def available() -> bool:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305  # noqa: F401
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF  # noqa: F401
    except Exception:
        return False
    return True


@dataclass(frozen=True)
class Keys:
    kid: str
    push: bytes
    answer: bytes
    meta: bytes


def b64u(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def unb64u(value: str) -> bytes:
    if not isinstance(value, str) or not _B64URL_PATTERN.match(value) or len(value) % 4 == 1:
        raise E2EError("malformed base64url")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def valid_kid(kid: Any) -> bool:
    return isinstance(kid, str) and bool(KID_PATTERN.match(kid))


def derive_keys(kid: str, secret: bytes) -> Keys:
    if not valid_kid(kid):
        raise E2EError("malformed key id")
    if len(secret) != SECRET_BYTES:
        raise E2EError("the pairing secret must be 32 bytes")
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    except Exception as error:
        raise E2EError("the cryptography package is not available") from error

    def derive(info: bytes) -> bytes:
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=SALT, info=info).derive(secret)

    return Keys(kid=kid, push=derive(INFO_PUSH), answer=derive(INFO_ANSWER), meta=derive(INFO_META))


def keys_from_state(state: dict[str, Any]) -> Keys | None:
    """The pairing's keys, or None when this pairing never provisioned E2E.

    Raises E2EError when a key IS provisioned but can't be used: the caller
    must then fail closed instead of falling back to plaintext.
    """
    record = state.get("e2e")
    if record is None:
        return None
    if not isinstance(record, dict) or not record:
        raise E2EError("the stored key is malformed")
    return derive_keys(str(record.get("kid") or ""), unb64u(str(record.get("secret") or "")))


def key_time(value: Any) -> float | None:
    """A key record's created_at or retired_at as a Unix time, or None."""
    from datetime import datetime, timezone

    try:
        return datetime.strptime(str(value), KEY_TIME_FORMAT).replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def previous_key_record(state: dict[str, Any], now: float | None = None) -> dict[str, Any] | None:
    """The key the last rotation replaced, while it still opens answers."""
    record = state.get("e2e_previous")
    if not isinstance(record, dict) or not valid_kid(record.get("kid")):
        return None
    retired = key_time(record.get("retired_at"))
    if retired is None or (now if now is not None else time.time()) >= retired + PREVIOUS_KEY_GRACE_S:
        return None
    return record


def answer_keys(state: dict[str, Any], now: float | None = None) -> list[Keys]:
    """The keys a clarify answer may be sealed with: the pairing's key, and
    during the grace period the one it replaced. Raises E2EError like
    keys_from_state when the current key is unusable."""
    current = keys_from_state(state)
    if current is None:
        return []
    keys = [current]
    previous = previous_key_record(state, now)
    if previous is not None:
        try:
            keys.append(derive_keys(str(previous["kid"]), unb64u(str(previous.get("secret") or ""))))
        except E2EError:
            pass  # a broken old key only means old answers no longer open
    return keys


def rotation_due(state: dict[str, Any], now: float | None = None) -> bool:
    """Whether the phone should hand over a new key: asked for, or the key
    is older than KEY_MAX_AGE_S (or has no usable created_at)."""
    record = state.get("e2e")
    if not isinstance(record, dict):
        return False
    if state.get("rotate_requested_at"):
        return True
    created = key_time(record.get("created_at"))
    return created is None or (now if now is not None else time.time()) >= created + KEY_MAX_AGE_S


def keyed_event_id(keys: Keys, event_id: str) -> str:
    prefix = event_id.split(":", 1)[0] if ":" in event_id else "event"
    digest = hmac.new(keys.meta, event_id.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    return f"{prefix}:{digest}"


def thread_token(keys: Keys, seed: str) -> str:
    return hmac.new(keys.meta, b"thread\n" + seed.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def push_aad(*, kid: str, installation_id: str, gateway_id: str, msg: str, kind: str,
             iat: int, tok: str, z: int, request_id: str) -> bytes:
    return "\n".join([
        AAD_TAG, "push", f"kid={kid}", f"inst={installation_id}", f"gw={gateway_id}",
        f"msg={msg}", f"type={kind}", f"iat={iat}", f"tok={tok}", f"z={z}", f"req={request_id}",
    ]).encode("utf-8")


def answer_aad(*, kid: str, installation_id: str, gateway_id: str, request_id: str, question_id: str) -> bytes:
    return "\n".join([
        AAD_TAG, "answer", f"kid={kid}", f"inst={installation_id}", f"gw={gateway_id}",
        f"req={request_id}", f"qid={question_id}",
    ]).encode("utf-8")


def _aead(key: bytes) -> Any:
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    return ChaCha20Poly1305(key)


def _pack(inner: dict[str, Any]) -> tuple[bytes, int]:
    raw = json.dumps(inner, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    # Raw deflate (no zlib header) is what Apple's Compression framework
    # reads as COMPRESSION_ZLIB.
    compressor = zlib.compressobj(level=9, wbits=-15)
    packed = compressor.compress(raw) + compressor.flush()
    return (packed, 1) if len(packed) < len(raw) else (raw, 0)


def unpack(data: bytes, z: int) -> dict[str, Any]:
    if z == 1:
        decompressor = zlib.decompressobj(wbits=-15)
        # Bounded: a valid envelope never inflates past this.
        try:
            data = decompressor.decompress(data, 64 * 1024)
        except zlib.error as error:
            raise E2EError("inner payload is not valid deflate") from error
        if decompressor.unconsumed_tail:
            raise E2EError("inner payload too large")
        if not decompressor.eof:
            raise E2EError("inner payload is truncated")
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise E2EError("inner payload is not JSON") from error
    if not isinstance(value, dict):
        raise E2EError("inner payload is not an object")
    return value


def seal_event(
    event: dict[str, Any],
    keys: Keys,
    *,
    installation_id: str,
    gateway_id: str,
    now: float | None = None,
) -> dict[str, Any]:
    """The outgoing event with its content sealed into an `e2e` envelope.

    Visible to the relay: type, re-keyed event id, plugin version and
    capabilities, a phone change's wake mode, and for clarify decisions the
    request id, question ids and whether the card fit. Everything in INNER_FIELDS is encrypted.
    """
    kind = str(event.get("type") or "")
    msg = keyed_event_id(keys, str(event.get("event_id") or ""))
    iat = int(now if now is not None else time.time())
    tok = thread_token(keys, str(event.get("session_id") or msg))
    inner = {field: event[field] for field in INNER_FIELDS if event.get(field) not in (None, "")}

    outgoing: dict[str, Any] = {"event_id": msg, "type": kind}
    _copy_routing(event, outgoing)

    decision = inner.get("decision") if isinstance(inner.get("decision"), dict) else None
    request_id = ""
    clarify: dict[str, Any] | None = None
    if decision and decision.get("kind") == "clarify" and decision.get("request_id"):
        request_id = str(decision["request_id"])
        questions = decision.get("questions") if isinstance(decision.get("questions"), list) else []
        clarify = {
            "request_id": request_id,
            "qids": [str(entry.get("qid")) for entry in questions if isinstance(entry, dict) and entry.get("qid")],
            "card": True,
        }

    def attempt(content: dict[str, Any]) -> dict[str, Any] | None:
        packed, z = _pack(content)
        nonce = os.urandom(NONCE_BYTES)
        aad = push_aad(kid=keys.kid, installation_id=installation_id, gateway_id=gateway_id,
                       msg=msg, kind=kind, iat=iat, tok=tok, z=z, request_id=request_id)
        ct = b64u(_aead(keys.push).encrypt(nonce, packed, aad))
        if len(ct) > MAX_CT_CHARS:
            return None
        return {"v": VERSION, "kid": keys.kid, "msg": msg, "iat": iat, "tok": tok, "z": z, "req": request_id,
                "n": b64u(nonce), "ct": ct}

    envelope = attempt(inner)
    if envelope is None and decision is not None:
        # The card doesn't fit one push. Send the banner without it; the
        # relay parks the decision as undeliverable, so the clarify loop falls
        # back to Hermes' own clarify path on its first poll (as before E2E).
        inner = {key: value for key, value in inner.items() if key != "decision"}
        if clarify is not None:
            clarify["card"] = False
        envelope = attempt(inner)
    if envelope is None:
        # Only routing survives; the phone shows the generic banner.
        inner = {key: value for key, value in inner.items() if key in ("session_id", "profile", "stored_session_id", "call",
                                                                     "locate", "maps")}
        envelope = attempt(inner)
    if envelope is None:
        raise E2EError("the event does not fit an encrypted push")
    outgoing["e2e"] = envelope
    if clarify is not None:
        outgoing["clarify"] = clarify
    return outgoing


def content_free(event: dict[str, Any], keys: Keys | None = None) -> dict[str, Any]:
    """The event with every content field removed.

    Used when a pairing has a key but sealing failed: a provisioned pairing
    never falls back to plaintext content.
    """
    event_id = str(event.get("event_id") or "")
    outgoing: dict[str, Any] = {
        "event_id": keyed_event_id(keys, event_id) if keys else event_id,
        "type": event.get("type"),
    }
    _copy_routing(event, outgoing)
    if event.get("type") == "location.requested" and isinstance(event.get("locate"), dict):
        # Only the request id the phone answers with: routing, not content.
        locate_id = event["locate"].get("id")
        if isinstance(locate_id, str) and _LOCATE_ID.match(locate_id):
            outgoing["locate"] = {"id": locate_id}
    if event.get("type") == "location.requested" and isinstance(event.get("maps"), dict):
        # A maps lookup's id, the same way: the phone fetches the lookup.
        maps_id = event["maps"].get("id")
        if isinstance(maps_id, str) and _MAPS_ID.match(maps_id):
            outgoing["maps"] = {"id": maps_id}
    return outgoing


def _copy_routing(event: dict[str, Any], outgoing: dict[str, Any]) -> None:
    """The fields the relay routes on, which stay outside the envelope: the
    plugin's version and capabilities, and for phone.changes how to wake the
    phone (``mode``: alert or background)."""
    for field in ("plugin_version", "plugin_capabilities"):
        if field in event:
            outgoing[field] = event[field]
    if event.get("type") == "phone.changes" and event.get("mode") in ("alert", "background"):
        outgoing["mode"] = event["mode"]


def open_event(outgoing: dict[str, Any], keys: Keys, *, installation_id: str, gateway_id: str) -> dict[str, Any]:
    """Device-side mirror of seal_event; used by tests and diagnostics."""
    envelope = outgoing.get("e2e")
    try:
        z = int(envelope["z"])
        aad = push_aad(kid=keys.kid, installation_id=installation_id, gateway_id=gateway_id,
                       msg=envelope["msg"], kind=str(outgoing.get("type") or ""), iat=int(envelope["iat"]),
                       tok=envelope["tok"], z=z, request_id=str(envelope.get("req") or ""))
        nonce, ciphertext = unb64u(envelope["n"]), unb64u(envelope["ct"])
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise E2EError("the envelope is malformed") from error
    try:
        packed = _aead(keys.push).decrypt(nonce, ciphertext, aad)
    except E2EError:
        raise
    except Exception as error:
        raise E2EError("the envelope did not verify") from error
    return unpack(packed, z)


def seal_answer(answer: str, keys: Keys, *, installation_id: str, gateway_id: str,
                request_id: str, question_id: str = "") -> str:
    """Device-side answer sealing; used by tests (Conduit does this on the phone)."""
    nonce = os.urandom(NONCE_BYTES)
    aad = answer_aad(kid=keys.kid, installation_id=installation_id, gateway_id=gateway_id,
                     request_id=request_id, question_id=question_id)
    ct = _aead(keys.answer).encrypt(nonce, answer.encode("utf-8"), aad)
    return f"{ANSWER_PREFIX}{keys.kid}.{b64u(nonce)}.{b64u(ct)}"


def open_answer_any(sealed: Any, keys: list[Keys], *, installation_id: str, gateway_id: str,
                    request_id: str, question_id: str = "") -> str:
    """open_answer with whichever of ``keys`` the answer names."""
    kid = sealed[len(ANSWER_PREFIX):].split(".", 1)[0] if isinstance(sealed, str) and sealed.startswith(ANSWER_PREFIX) else ""
    chosen = next((key for key in keys if key.kid == kid), keys[0] if keys else None)
    if chosen is None:
        raise E2EError("this pairing has no key")
    return open_answer(sealed, chosen, installation_id=installation_id, gateway_id=gateway_id,
                       request_id=request_id, question_id=question_id)


def open_answer(sealed: Any, keys: Keys, *, installation_id: str, gateway_id: str,
                request_id: str, question_id: str = "") -> str:
    """The answer text, or E2EError for plaintext, a foreign key or a forgery."""
    if not isinstance(sealed, str) or not sealed.startswith(ANSWER_PREFIX):
        raise E2EError("the answer is not encrypted")
    parts = sealed[len(ANSWER_PREFIX):].split(".")
    if len(parts) != 3 or parts[0] != keys.kid:
        raise E2EError("the answer was sealed with another key")
    aad = answer_aad(kid=keys.kid, installation_id=installation_id, gateway_id=gateway_id,
                     request_id=request_id, question_id=question_id)
    try:
        plain = _aead(keys.answer).decrypt(unb64u(parts[1]), unb64u(parts[2]), aad)
    except E2EError:
        raise
    except Exception as error:
        raise E2EError("the answer did not verify") from error
    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError as error:
        raise E2EError("the answer is not valid text") from error


# --- Inline key rotation (conduit-e2e/2) ---------------------------------------
#
# Every later key comes from a key exchange done inside messages sealed with
# the current key, so only a pairing's first key depends on the dashboard
# connection that carried it. The phone sends a fresh X25519 share and, on
# iOS 26 and later, an ML-KEM-768 encapsulation key, sealed with the answer
# key. The host answers with its X25519 share and the ML-KEM ciphertext,
# sealed with the push key. The new secret mixes both shared secrets with the
# old secret, so a recording of the first handover stops being useful after
# the first rotation (ML-KEM), and a leaked key stops opening anything after
# the next one (both suites).

SUITE_PQ = "mlkem768+x25519"
SUITE_CLASSICAL = "x25519"
ROTATE_SALT = b"conduit-e2e-v2 rotate"
ROTATE_AAD_TAG = "conduit-e2e/2"
MLKEM768_EK_BYTES = 1184
MLKEM768_CT_BYTES = 1088
X25519_BYTES = 32


def rotate_suites() -> list[str]:
    """The rotation suites this host can run, best first."""
    suites = []
    try:
        from cryptography.hazmat.primitives.asymmetric import mlkem  # noqa: F401

        mlkem.MLKEM768PrivateKey.generate()
        suites.append(SUITE_PQ)
    except Exception:
        pass
    try:
        from cryptography.hazmat.primitives.asymmetric import x25519  # noqa: F401

        suites.append(SUITE_CLASSICAL)
    except Exception:
        pass
    return suites


def rotate_request_aad(*, kid: str, installation_id: str, gateway_id: str, suite: str) -> bytes:
    return "\n".join([ROTATE_AAD_TAG, "rotate-request", f"kid={kid}", f"inst={installation_id}",
                      f"gw={gateway_id}", f"suite={suite}"]).encode("utf-8")


def rotate_reply_aad(*, kid: str, kid_new: str, installation_id: str, gateway_id: str, suite: str) -> bytes:
    return "\n".join([ROTATE_AAD_TAG, "rotate-reply", f"kid={kid}", f"kid_new={kid_new}", f"inst={installation_id}",
                      f"gw={gateway_id}", f"suite={suite}"]).encode("utf-8")


def rotation_info(*, kid: str, kid_new: str, installation_id: str, gateway_id: str, suite: str,
                  ek: bytes, xpub: bytes, host_xpub: bytes, ct: bytes) -> bytes:
    """HKDF info: every value both sides sent, so the two derive the same
    secret only from the same exchange."""
    return "\n".join([
        ROTATE_SALT.decode("ascii"), f"kid={kid}", f"kid_new={kid_new}", f"inst={installation_id}",
        f"gw={gateway_id}", f"suite={suite}", "ek=" + hashlib.sha256(ek).hexdigest(), "xpub=" + xpub.hex(),
        "host_xpub=" + host_xpub.hex(), "ct=" + hashlib.sha256(ct).hexdigest(),
    ]).encode("utf-8")


def rotated_secret(*, ss_pq: bytes, ss_x: bytes, old_secret: bytes, info: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return HKDF(algorithm=hashes.SHA256(), length=SECRET_BYTES, salt=ROTATE_SALT, info=info).derive(
        ss_pq + ss_x + old_secret)


def _seal_json(key: bytes, value: dict[str, Any], aad: bytes) -> dict[str, str]:
    nonce = os.urandom(NONCE_BYTES)
    raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
    return {"n": b64u(nonce), "ct": b64u(_aead(key).encrypt(nonce, raw, aad))}


def _open_json(key: bytes, sealed: Any, aad: bytes) -> dict[str, Any]:
    if not isinstance(sealed, dict):
        raise E2EError("the rotation message is malformed")
    try:
        plain = _aead(key).decrypt(unb64u(str(sealed.get("n") or "")), unb64u(str(sealed.get("ct") or "")), aad)
        value = json.loads(plain.decode("utf-8"))
    except E2EError:
        raise
    except Exception as error:
        raise E2EError("the rotation message did not verify") from error
    if not isinstance(value, dict):
        raise E2EError("the rotation message is malformed")
    return value


def _raw(value: Any, length: int, name: str) -> bytes:
    data = unb64u(value) if isinstance(value, str) else b""
    if len(data) != length:
        raise E2EError(f"{name} must be {length} bytes")
    return data


@dataclass(frozen=True)
class Rotation:
    kid_new: str
    secret: bytes
    suite: str
    reply: dict[str, Any]


def answer_rotation(request: dict[str, Any], keys: Keys, old_secret: bytes, *,
                    installation_id: str, gateway_id: str) -> Rotation:
    """Host side: opens the phone's rotate request (sealed with the current
    key), runs the key exchange and returns the new secret with the sealed
    reply."""
    suite = request.get("suite")
    if suite not in (SUITE_PQ, SUITE_CLASSICAL):
        raise E2EError("unknown rotation suite")
    if request.get("kid") != keys.kid:
        raise E2EError("the request was sealed with another key")
    inner = _open_json(keys.answer, request.get("sealed"), rotate_request_aad(
        kid=keys.kid, installation_id=installation_id, gateway_id=gateway_id, suite=suite))
    kid_new = inner.get("kid_new")
    if not valid_kid(kid_new) or kid_new == keys.kid or inner.get("suite") != suite:
        raise E2EError("the rotation request is malformed")
    xpub = _raw(inner.get("xpub"), X25519_BYTES, "xpub")
    from cryptography.hazmat.primitives.asymmetric import x25519

    host_x = x25519.X25519PrivateKey.generate()
    host_xpub = host_x.public_key().public_bytes_raw()
    try:
        ss_x = host_x.exchange(x25519.X25519PublicKey.from_public_bytes(xpub))
    except ValueError as error:
        raise E2EError("xpub is not a usable X25519 key") from error
    ek, ct, ss_pq = b"", b"", b""
    if suite == SUITE_PQ:
        from cryptography.hazmat.primitives.asymmetric import mlkem

        ek = _raw(inner.get("ek"), MLKEM768_EK_BYTES, "ek")
        try:
            ss_pq, ct = mlkem.MLKEM768PublicKey.from_public_bytes(ek).encapsulate()
        except ValueError as error:
            raise E2EError("ek is not a usable ML-KEM-768 key") from error
    info = rotation_info(kid=keys.kid, kid_new=kid_new, installation_id=installation_id, gateway_id=gateway_id,
                         suite=suite, ek=ek, xpub=xpub, host_xpub=host_xpub, ct=ct)
    secret = rotated_secret(ss_pq=ss_pq, ss_x=ss_x, old_secret=old_secret, info=info)
    content = {"kid_new": kid_new, "suite": suite, "host_xpub": b64u(host_xpub)}
    if ct:
        content["ct"] = b64u(ct)
    reply = {"kid": keys.kid, "kid_new": kid_new, "suite": suite,
             "sealed": _seal_json(keys.push, content, rotate_reply_aad(
                 kid=keys.kid, kid_new=kid_new, installation_id=installation_id, gateway_id=gateway_id, suite=suite))}
    return Rotation(kid_new=kid_new, secret=secret, suite=suite, reply=reply)


def start_rotation(keys: Keys, *, installation_id: str, gateway_id: str, suite: str,
                   kid_new: str) -> tuple[dict[str, Any], Any]:
    """Device-side mirror (Conduit does this on the phone); tests use it.
    Returns the request and the private state finish_rotation needs."""
    if suite not in (SUITE_PQ, SUITE_CLASSICAL):
        raise E2EError("unknown rotation suite")
    if not valid_kid(kid_new) or kid_new == keys.kid:
        raise E2EError("kid_new must be a new key id")
    from cryptography.hazmat.primitives.asymmetric import x25519

    x_priv = x25519.X25519PrivateKey.generate()
    inner: dict[str, Any] = {"kid_new": kid_new, "suite": suite, "xpub": b64u(x_priv.public_key().public_bytes_raw())}
    kem = None
    if suite == SUITE_PQ:
        from cryptography.hazmat.primitives.asymmetric import mlkem

        kem = mlkem.MLKEM768PrivateKey.generate()
        inner["ek"] = b64u(kem.public_key().public_bytes_raw())
    request = {"kid": keys.kid, "suite": suite, "installation_id": installation_id, "gateway_id": gateway_id,
               "sealed": _seal_json(keys.answer, inner, rotate_request_aad(
                   kid=keys.kid, installation_id=installation_id, gateway_id=gateway_id, suite=suite))}
    return request, (x_priv, kem, inner)


def finish_rotation(reply: dict[str, Any], keys: Keys, old_secret: bytes, pending: Any, *,
                    installation_id: str, gateway_id: str) -> bytes:
    """Device-side mirror: the new secret from the host's reply."""
    from cryptography.hazmat.primitives.asymmetric import x25519

    x_priv, kem, inner = pending
    suite, kid_new = inner["suite"], inner["kid_new"]
    content = _open_json(keys.push, reply.get("sealed"), rotate_reply_aad(
        kid=keys.kid, kid_new=kid_new, installation_id=installation_id, gateway_id=gateway_id, suite=suite))
    if content.get("kid_new") != kid_new or content.get("suite") != suite:
        raise E2EError("the rotation reply is for another request")
    host_xpub = _raw(content.get("host_xpub"), X25519_BYTES, "host_xpub")
    ss_x = x_priv.exchange(x25519.X25519PublicKey.from_public_bytes(host_xpub))
    ek, ct, ss_pq = b"", b"", b""
    if suite == SUITE_PQ:
        ct = _raw(content.get("ct"), MLKEM768_CT_BYTES, "ct")
        ek = unb64u(inner["ek"])
        ss_pq = kem.decapsulate(ct)
    info = rotation_info(kid=keys.kid, kid_new=kid_new, installation_id=installation_id, gateway_id=gateway_id,
                         suite=suite, ek=ek, xpub=unb64u(inner["xpub"]), host_xpub=host_xpub, ct=ct)
    return rotated_secret(ss_pq=ss_pq, ss_x=ss_x, old_secret=old_secret, info=info)
