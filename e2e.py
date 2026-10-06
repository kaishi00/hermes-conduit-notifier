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
INNER_FIELDS = ("title", "body", "session_id", "profile", "gateway", "stored_session_id", "decision")

KID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_B64URL_PATTERN = re.compile(r"^[A-Za-z0-9_-]*$")


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
    capabilities, and for clarify decisions the request id, question ids and
    whether the card fit. Everything in INNER_FIELDS is encrypted.
    """
    kind = str(event.get("type") or "")
    msg = keyed_event_id(keys, str(event.get("event_id") or ""))
    iat = int(now if now is not None else time.time())
    tok = thread_token(keys, str(event.get("session_id") or msg))
    inner = {field: event[field] for field in INNER_FIELDS if event.get(field) not in (None, "")}

    outgoing: dict[str, Any] = {"event_id": msg, "type": kind}
    for field in ("plugin_version", "plugin_capabilities"):
        if field in event:
            outgoing[field] = event[field]

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
        inner = {key: value for key, value in inner.items() if key in ("session_id", "profile", "stored_session_id")}
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
    for field in ("plugin_version", "plugin_capabilities"):
        if field in event:
            outgoing[field] = event[field]
    return outgoing


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
