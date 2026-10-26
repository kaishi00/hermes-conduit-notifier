"""Envelopes between linked installations, for Linked Rooms (designs/cross-owner-agents, decision 5).

What one installation sends another through the rooms mailbox is an
envelope: JSON with a header in the clear (the format version, the Link, the
sender's and the recipient's inboxes, a message id, its kind and the
sender's time) and a body sealed to the recipient's X25519 key with a fresh
ephemeral key, the whole signed with the sender's Ed25519 key. The mailbox
holds only these bytes. It can see who sends to whom, how much and when, but
not what, and it can't change or forge an envelope without the recipient
refusing it. The message id doubles as the mailbox's Idempotency-Key, and
whoever handles an envelope makes doing it twice harmless.

A Link's first message comes from someone whose key the recipient doesn't
know yet. Its sender's signing key rides inside the sealed body, and must be
the key its inbox is named after. Nothing here imports the rest of the plugin
but room_identity.py and sealing.py.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from . import room_identity, sealing

VERSION = 1
# The mailbox protocol's limit (mailbox/src/limits.mjs).
MAX_BYTES = 262_144
# 2100-01-01: later (or inf) is a corrupt time.
MAX_AT = 4_102_444_800
LABEL = b"conduit-rooms-envelope-v1"

LINK_ID = re.compile(r"^[A-Za-z0-9_-]{1,48}\Z")
INBOX_ID = re.compile(r"^[A-Za-z0-9_-]{43}\Z")
# The mailbox's Idempotency-Key.
MESSAGE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}\Z")
KIND = re.compile(r"^[a-z][a-z0-9_.]{0,63}\Z")
_PUBLIC_KEY = re.compile(r"^[A-Za-z0-9_-]{43}\Z")
_NONCE = re.compile(r"^[A-Za-z0-9_-]{16}\Z")
_SIGNATURE = re.compile(r"^[A-Za-z0-9_-]{86}\Z")
_BASE64URL = re.compile(r"^[A-Za-z0-9_-]+\Z")
_FIELDS = frozenset({"v", "link", "from", "to", "id", "kind", "at", "epk", "nonce", "ct", "sig"})


class EnvelopeError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class Header:
    link: str
    sender: str
    recipient: str
    id: str
    kind: str
    at: int


@dataclass(frozen=True)
class Opened:
    header: Header
    body: dict
    # The key the signature was checked with: the Link's, or for its first
    # message, the one inside the body.
    sender_sign: bytes


def new_message_id() -> str:
    return sealing.b64u(secrets.token_bytes(16))


def seal(identity: room_identity.Identity, *, link: str, recipient_sign: bytes, recipient_seal: bytes,
         message_id: str, kind: str, body: dict, at: Optional[int] = None,
         clock: Callable[[], float] = time.time) -> bytes:
    """``body`` (a JSON object) sealed to the recipient and signed by
    ``identity``, as the bytes to send."""
    if not isinstance(body, dict):
        raise EnvelopeError("malformed", "an envelope's body is a JSON object")
    if len(recipient_sign) != room_identity.KEY_BYTES or len(recipient_seal) != room_identity.KEY_BYTES:
        raise EnvelopeError("malformed", "the recipient's keys are 32 bytes each")
    header = Header(link=link, sender=identity.inbox, recipient=room_identity.inbox_id(recipient_sign),
                    id=message_id, kind=kind, at=int(clock()) if at is None else at)
    _check(header)
    ephemeral_private, ephemeral_public = sealing.new_host_keypair()
    epk = sealing.b64u(ephemeral_public)
    key = _body_key(sealing.agree(ephemeral_private, bytes(recipient_seal), EnvelopeError), ephemeral_public,
                    bytes(recipient_seal))
    plaintext = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    nonce, ciphertext = sealing.seal(key, plaintext, _aad(header, epk))
    signature = identity.sign(_signed(header, epk, nonce, ciphertext))
    data = json.dumps({
        "v": VERSION, "link": header.link, "from": header.sender, "to": header.recipient, "id": header.id,
        "kind": header.kind, "at": header.at, "epk": epk, "nonce": nonce, "ct": ciphertext,
        "sig": sealing.b64u(signature),
    }, separators=(",", ":")).encode("ascii")
    if len(data) > MAX_BYTES:
        raise EnvelopeError("too_large", f"the envelope is {len(data)} bytes; the mailbox takes {MAX_BYTES}", 413)
    return data


def peek(data: bytes) -> Header:
    """The header, unchecked: who it says it's from, so the Link can be
    found. Nothing in it counts until open_envelope checks it."""
    return _parse(data)[0]


def open_envelope(identity: room_identity.Identity, data: bytes, sender_sign: Optional[bytes] = None) -> Opened:
    """The checked header and the body. ``sender_sign`` is the Link's signing
    key for the sender; None for a Link's first message, whose key is inside
    the body."""
    header, fields = _parse(data)
    if header.recipient != identity.inbox:
        raise EnvelopeError("not_for_me", "the envelope is for another installation's inbox")
    epk, nonce, ciphertext = fields["epk"], fields["nonce"], fields["ct"]
    signed = _signed(header, epk, nonce, ciphertext)
    signature = sealing.unb64u(fields["sig"], EnvelopeError)
    if sender_sign is not None:
        if room_identity.inbox_id(sender_sign) != header.sender:
            raise EnvelopeError("wrong_sender", "the envelope isn't from the installation this Link is with")
        if not room_identity.verify(sender_sign, signed, signature):
            raise EnvelopeError("bad_signature", "the envelope's signature doesn't check")
    body = _open_body(identity, header, epk, nonce, ciphertext)
    if sender_sign is None:
        inner = body.get("sign")
        sender_sign = sealing.unb64u(inner, EnvelopeError) if isinstance(inner, str) and _PUBLIC_KEY.match(inner) \
            else None
        if sender_sign is None or room_identity.inbox_id(sender_sign) != header.sender:
            raise EnvelopeError("wrong_sender", "the key inside isn't the one the sender's inbox is named after")
        if not room_identity.verify(sender_sign, signed, signature):
            raise EnvelopeError("bad_signature", "the envelope's signature doesn't check")
    return Opened(header=header, body=body, sender_sign=bytes(sender_sign))


# --- Helpers ---------------------------------------------------------------------


def _check(header: Header) -> None:
    if not isinstance(header.link, str) or not LINK_ID.match(header.link):
        raise EnvelopeError("malformed", "link is 1 to 48 letters, digits, _ or -")
    for name, value in (("from", header.sender), ("to", header.recipient)):
        if not isinstance(value, str) or not INBOX_ID.match(value):
            raise EnvelopeError("malformed", f"{name} is an inbox id")
    if not isinstance(header.id, str) or not MESSAGE_ID.match(header.id):
        raise EnvelopeError("malformed", "id is 1 to 128 letters, digits, _ . : or -")
    if not isinstance(header.kind, str) or not KIND.match(header.kind):
        raise EnvelopeError("malformed", "kind is a lowercase name")
    if type(header.at) is not int or not 0 <= header.at <= MAX_AT:
        raise EnvelopeError("malformed", "at is a time in Unix seconds")


def _parse(data: bytes) -> tuple[Header, dict]:
    if len(data) > MAX_BYTES:
        raise EnvelopeError("too_large", f"an envelope is at most {MAX_BYTES} bytes", 413)
    try:
        fields = json.loads(data)
    except (ValueError, RecursionError):  # bytes that aren't UTF-8 included
        raise EnvelopeError("malformed", "an envelope is a JSON object") from None
    if not isinstance(fields, dict) or set(fields) != _FIELDS:
        raise EnvelopeError("malformed", "an envelope has the fields " + ", ".join(sorted(_FIELDS)))
    if type(fields["v"]) is not int or fields["v"] != VERSION:
        raise EnvelopeError("malformed", f"this plugin reads envelopes of version {VERSION}")
    header = Header(link=fields["link"], sender=fields["from"], recipient=fields["to"], id=fields["id"],
                    kind=fields["kind"], at=fields["at"])
    _check(header)
    for name, pattern in (("epk", _PUBLIC_KEY), ("nonce", _NONCE), ("ct", _BASE64URL), ("sig", _SIGNATURE)):
        if not isinstance(fields[name], str) or not pattern.match(fields[name]):
            raise EnvelopeError("malformed", f"{name} is base64url of the right size")
    return header, fields


def _header_lines(header: Header, epk: str) -> bytes:
    # Every field is from a set without newlines, so the lines can't run
    # into each other.
    return b"\n".join([LABEL, str(VERSION).encode(), header.link.encode(), header.sender.encode(),
                       header.recipient.encode(), header.id.encode(), header.kind.encode(),
                       str(header.at).encode(), epk.encode()])


def _aad(header: Header, epk: str) -> bytes:
    return _header_lines(header, epk)


def _signed(header: Header, epk: str, nonce: str, ciphertext: str) -> bytes:
    return _header_lines(header, epk) + b"\n" + nonce.encode() + b"\n" + ciphertext.encode()


def _body_key(shared: bytes, ephemeral_public: bytes, recipient_seal: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return HKDF(algorithm=hashes.SHA256(), length=sealing.KEY_BYTES, salt=hashlib.sha256(LABEL).digest(),
                info=LABEL + b"\nbody\n" + ephemeral_public + recipient_seal).derive(shared)


def _open_body(identity: room_identity.Identity, header: Header, epk: str, nonce: str, ciphertext: str) -> dict:
    ephemeral_public = sealing.unb64u(epk, EnvelopeError)
    try:
        shared = sealing.agree(identity.sealing_key, ephemeral_public, EnvelopeError)
    except EnvelopeError:
        raise EnvelopeError("bad_seal", "the envelope's body wasn't sealed to this installation") from None
    key = _body_key(shared, ephemeral_public, identity.sealing_public)
    try:
        plaintext = sealing.open_sealed(key, nonce, ciphertext, _aad(header, epk), EnvelopeError,
                                        "it wasn't sealed to this installation")
    except EnvelopeError as exc:
        if exc.code == "bad_seal":
            raise EnvelopeError("bad_seal", "the envelope's body wasn't sealed to this installation") from None
        raise
    try:
        body = json.loads(plaintext)
    except (ValueError, RecursionError):
        body = None
    if not isinstance(body, dict):
        raise EnvelopeError("malformed", "the envelope's body isn't a JSON object")
    return body
