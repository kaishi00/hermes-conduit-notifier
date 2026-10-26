"""The rooms mailbox's client, for Linked Rooms (designs/cross-owner-agents; protocol v1 in mailbox/README.md).

An installation reads its own inbox with requests signed by its identity's
key (``Mailbox``), and leaves envelopes in a linked installation's inbox with
the send token that installation gave it (``send``). The mailbox holds only
sealed envelopes (room_envelope.py). Requests go out with urllib through the
host's proxy settings, as the push relay's do, and never follow a redirect:
a signature is for one host and path, and a send token is for one mailbox.

A refused owner request whose answer is dated far from this host's clock is
reported as the clock (``clock_off``), not as a dead link (review focus 3).
Nothing here imports the rest of the plugin but room_identity.py and
sealing.py.
"""

from __future__ import annotations

import base64
import binascii
import email.utils
import hashlib
import http.client
import ipaddress
import json
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

from . import room_identity, sealing

DEFAULT_URL = "https://rooms.milim.dev"
# How far an owner request's time may be from the mailbox's (mailbox/src/auth.mjs).
SIGNATURE_WINDOW_S = 300
# A refusal from a mailbox whose clock is this far from this host's is the
# clock's: past the window, less what a slow request might take.
CLOCK_SLACK_S = SIGNATURE_WINDOW_S - 30
MAX_WAIT_S = 25
TIMEOUT_S = 15.0
# The mailbox stops a page at 1 MB of envelopes, and protocol v1's are at most
# 256 KB, so a read's answer is at most about 1.4 MB in base64 however many
# large envelopes are waiting.
MAX_ANSWER_BYTES = 4 * 1024 * 1024
MAX_REFUSAL_BYTES = 4096
ACK_MAX_SEQS = 100

SENDER_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}\Z")
INBOX_ID = re.compile(r"^[A-Za-z0-9_-]{43}\Z")
IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9_.:-]{1,128}\Z")
SEND_TOKEN = re.compile(r"^[\x21-\x7e]{1,512}\Z")
# The mailbox's own MAILBOX_REGISTRATION_TOKEN rule.
REGISTRATION_TOKEN = re.compile(r"^[\x21-\x7e]{16,256}\Z")
_CODE = re.compile(r"^[a-z_]{1,64}\Z")
_NUMERIC_LABEL = re.compile(r"^(?:\d+|0[xX][0-9A-Fa-f]*)\Z")

_MESSAGES = {
    "unauthorized": "The mailbox didn't accept this request's signature or send token",
    "rate_limited": "The mailbox is busy; it takes requests again in a minute",
    "inbox_full": "The other installation's inbox is full until its Hermes reads it",
    "sender_backlog": "The other installation hasn't read what this one already sent; it can take more once it does",
    "too_many_waiters": "Too many reads are waiting at the mailbox; try again soon",
    "envelope_too_large": "That's more than the mailbox takes in one envelope (256 KB)",
    "payload_too_large": "That request is more than the mailbox takes",
    "registration_closed": "This mailbox makes inboxes only for installations that have its registration token",
    "mailbox_full": "The mailbox is out of space for now",
    "not_found": "The mailbox doesn't have that",
    "token_in_use": "Another link of this inbox has that send token",
    "too_many_senders": "This inbox has as many links as the mailbox allows",
}
_RETRYABLE_STATUSES = frozenset({429, 502, 503, 504, 507})


class MailboxError(Exception):
    def __init__(self, code: str, message: str, status: int = 0, server_time: Optional[float] = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        # The answer's Date, for telling a skewed clock from a refusal.
        self.server_time = server_time

    @property
    def retryable(self) -> bool:
        """Whether the same request may work later without anything changed."""
        return self.code in ("unreachable", "bad_answer", "clock_off") or self.status in _RETRYABLE_STATUSES


@dataclass(frozen=True)
class Delivered:
    seq: int
    sender: str
    received_at: int
    body: bytes


def base_url(value: Any) -> str:
    """``value`` as a mailbox URL: https at the root of an ASCII host name,
    or http only on this host (tests, a mailbox run beside Hermes). Raises
    MailboxError("bad_url") for anything else."""
    refused = MailboxError("bad_url", "A mailbox's URL is https://, with nothing after its host name (like "
                                      f"{DEFAULT_URL}), or http:// only on this host")
    if not isinstance(value, str) or len(value) > 2048 or "?" in value or "#" in value or not value.isascii():
        raise refused
    try:
        parts = urllib.parse.urlsplit(value.strip())
        port = parts.port
    except ValueError:
        raise refused from None
    host = parts.hostname
    if parts.scheme not in ("https", "http") or not host or parts.username is not None \
            or parts.password is not None or parts.path not in ("", "/") or port == 0:
        raise refused
    canonical = _canonical_host(host)
    if canonical is None or (parts.scheme == "http" and not _on_this_host(canonical)):
        raise refused
    return f"{parts.scheme}://{_host_and_port(parts.scheme, canonical, port)}"


def url_host(url: str) -> str:
    """The host an owner signs: as a URL parser gives it, lowercase, with
    the port only when it isn't the scheme's."""
    parts = urllib.parse.urlsplit(url)
    canonical = _canonical_host(parts.hostname or "")
    if canonical is None:
        raise MailboxError("bad_url", f"{url} has no host this plugin can sign")
    return _host_and_port(parts.scheme, canonical, parts.port)


def signed_string(method: str, host: str, target: str, ts: int, nonce: str, body: bytes) -> bytes:
    digest = sealing.b64u(hashlib.sha256(bytes(body)).digest())
    return f"mailbox-v1\n{method}\n{host}\n{target}\n{ts}\n{nonce}\n{digest}".encode("utf-8")


def owner_authorization(identity: room_identity.Identity, method: str, host: str, target: str, body: bytes, *,
                        ts: int, nonce: Optional[str] = None) -> str:
    # Ed25519 signs the same bytes the same way, so a fresh nonce is what
    # keeps a retry, or a second poll in one second, from reading as a replay.
    nonce = nonce if nonce is not None else sealing.b64u(secrets.token_bytes(18))
    signature = sealing.b64u(identity.sign(signed_string(method, host, target, ts, nonce, body)))
    return f"Mailbox-Ed25519 ts={ts},nonce={nonce},sig={signature}"


def token_sha256(token: str) -> str:
    return sealing.b64u(hashlib.sha256(token.encode("utf-8")).digest())


def new_token() -> str:
    return sealing.b64u(secrets.token_bytes(32))


class Mailbox:
    """This installation's own inbox at the mailbox at ``url``."""

    def __init__(self, url: str, identity: room_identity.Identity, *, registration_token: Optional[str] = None,
                 clock: Callable[[], float] = time.time, timeout: float = TIMEOUT_S) -> None:
        self.url = base_url(url)
        self.host = url_host(self.url)
        self.identity = identity
        self.registration_token = registration_token
        self.clock = clock
        self.timeout = timeout

    @property
    def _inbox_path(self) -> str:
        return f"/v1/inboxes/{self.identity.inbox}"

    def register(self) -> dict[str, Any]:
        """Makes the inbox, or confirms it: ``{inbox, limits, created}``."""
        if self.registration_token and not REGISTRATION_TOKEN.match(self.registration_token):
            raise MailboxError("bad_request", "a registration token is 16 to 256 printable ASCII characters, "
                                              "with no spaces")
        headers = {"Mailbox-Registration": self.registration_token} if self.registration_token else None
        status, answer = self._owner("POST", "/v1/inboxes", {"key": sealing.b64u(self.identity.signing_public)},
                                     headers=headers)
        limits = answer.get("limits")
        if answer.get("inbox") != self.identity.inbox or not isinstance(limits, dict):
            raise MailboxError("bad_answer", f"The mailbox at {self.host} answered a registration oddly")
        return {"inbox": self.identity.inbox, "limits": limits, "created": status == 201}

    def info(self) -> dict[str, Any]:
        return self._owner("GET", self._inbox_path)[1]

    def delete(self) -> None:
        self._owner("DELETE", self._inbox_path)

    def put_sender(self, sender: str, token: str) -> None:
        """Lets whoever holds ``token`` leave envelopes here as ``sender``."""
        _check_sender(sender)
        if not isinstance(token, str) or not SEND_TOKEN.match(token):
            raise MailboxError("bad_request", "a send token is 1 to 512 printable ASCII characters")
        self._owner("PUT", f"{self._inbox_path}/senders/{sender}", {"token_sha256": token_sha256(token)})

    def delete_sender(self, sender: str) -> bool:
        """Removes the sender with what it left; False when there was none."""
        _check_sender(sender)
        try:
            self._owner("DELETE", f"{self._inbox_path}/senders/{sender}")
        except MailboxError as exc:
            if exc.code == "not_found":
                return False
            raise
        return True

    def senders(self) -> list[dict[str, Any]]:
        senders = self._owner("GET", f"{self._inbox_path}/senders")[1].get("senders")
        if not isinstance(senders, list) or not all(isinstance(row, dict) for row in senders):
            raise MailboxError("bad_answer", f"The mailbox at {self.host} answered a list of senders oddly")
        return senders

    def fetch(self, after: int = 0, limit: int = 50, wait: int = 0) -> tuple[list[Delivered], int]:
        """Envelopes after seq ``after``, oldest first, and the seq to read
        after next. With ``wait`` (seconds, at most 25), an empty inbox
        holds the request until an envelope arrives or the wait ends."""
        if not _whole(after) or not _whole(limit) or limit < 1 or not _whole(wait):
            raise MailboxError("bad_request", "a read takes whole numbers: the seq to read after, a limit of 1 "
                                              "or more and the seconds to wait")
        wait = min(wait, MAX_WAIT_S)
        target = f"{self._inbox_path}/envelopes?after={after}&limit={limit}"
        if wait:
            target += f"&wait={wait}"
        answer = self._owner("GET", target, timeout=self.timeout + wait)[1]
        rows, following = answer.get("envelopes"), answer.get("next")
        if not isinstance(rows, list) or not _whole(following):
            raise MailboxError("bad_answer", f"The mailbox at {self.host} answered a read oddly")
        return [_delivered(row, self.host) for row in rows], following

    def ack(self, through: Optional[int] = None, seqs: Optional[Iterable[int]] = None) -> int:
        """Deletes the envelopes up to and including ``through``, and those
        in ``seqs``: how many it deleted."""
        body: dict[str, Any] = {}
        if through is not None:
            body["through"] = through
        if seqs is not None:
            body["seqs"] = list(seqs)
        valid_through = through is None or _whole(through)
        valid_seqs = seqs is None or (1 <= len(body["seqs"]) <= ACK_MAX_SEQS
                                      and all(_whole(seq) and seq > 0 for seq in body["seqs"]))
        if not body or not valid_through or not valid_seqs:
            raise MailboxError("bad_request", "an ack names a whole number to delete through, or 1 to "
                                              f"{ACK_MAX_SEQS} seqs of 1 or more")
        deleted = self._owner("POST", f"{self._inbox_path}/ack", body)[1].get("deleted")
        if not _whole(deleted):
            raise MailboxError("bad_answer", f"The mailbox at {self.host} answered an ack oddly")
        return deleted

    def _owner(self, method: str, target: str, payload: Optional[dict] = None, *,
               headers: Optional[dict[str, str]] = None, timeout: Optional[float] = None) -> tuple[int, dict]:
        body = b"" if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
        ts = int(self.clock())
        request_headers = {"Authorization": owner_authorization(self.identity, method, self.host, target, body,
                                                                ts=ts)}
        if payload is not None:
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        try:
            return _request(self.url + target, method, body if payload is not None else None, request_headers,
                            self.timeout if timeout is None else timeout, self.host)
        except MailboxError as exc:
            if exc.status == 401 and exc.server_time is not None and abs(exc.server_time - ts) > CLOCK_SLACK_S:
                raise MailboxError("clock_off", _clock_off(exc.server_time - ts), 401, exc.server_time) from None
            raise


def send(url: str, inbox: str, token: str, body: bytes, idempotency_key: Optional[str] = None, *,
         timeout: float = TIMEOUT_S) -> int:
    """Leaves ``body`` in a linked installation's inbox with the send token
    it gave this one: when the mailbox drops it if it isn't read. Sending
    again with the same ``idempotency_key`` while the first is waiting
    stores no copy."""
    base = base_url(url)
    if not isinstance(inbox, str) or not INBOX_ID.match(inbox):
        raise MailboxError("bad_request", "an inbox id is 43 base64url characters")
    if not isinstance(token, str) or not SEND_TOKEN.match(token):
        raise MailboxError("bad_request", "a send token is 1 to 512 printable ASCII characters")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/octet-stream"}
    if idempotency_key is not None:
        if not isinstance(idempotency_key, str) or not IDEMPOTENCY_KEY.match(idempotency_key):
            raise MailboxError("bad_request", "an idempotency key is 1 to 128 letters, digits, _ . : or -")
        headers["Idempotency-Key"] = idempotency_key
    host = url_host(base)
    expires_at = _request(f"{base}/v1/inboxes/{inbox}/envelopes", "POST", bytes(body), headers, timeout,
                          host)[1].get("expires_at")
    if not _whole(expires_at):
        raise MailboxError("bad_answer", f"The mailbox at {host} answered a send oddly")
    return expires_at


# --- Helpers ---------------------------------------------------------------------


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


_opener = urllib.request.build_opener(_NoRedirects)


def _request(url: str, method: str, data: Optional[bytes], headers: dict[str, str], timeout: float,
             host: str) -> tuple[int, dict]:
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with _opener.open(request, timeout=timeout) as answer:
            status = answer.status
            raw = answer.read(MAX_ANSWER_BYTES + 1)
    except urllib.error.HTTPError as error:
        raise _refusal(error, host) from None
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as error:
        reason = getattr(error, "reason", None) or error
        raise MailboxError("unreachable", f"Couldn't reach the mailbox at {host} ({reason})") from None
    if len(raw) > MAX_ANSWER_BYTES:
        raise MailboxError("bad_answer", f"The mailbox at {host} answered with more than this plugin reads")
    if not raw:
        return status, {}
    try:
        answer = json.loads(raw)
    except (ValueError, RecursionError):
        answer = None
    if not isinstance(answer, dict):
        raise MailboxError("bad_answer", f"The mailbox at {host} didn't answer like a rooms mailbox")
    return status, answer


def _refusal(error: urllib.error.HTTPError, host: str) -> MailboxError:
    try:
        raw = error.read(MAX_REFUSAL_BYTES)
    except (OSError, http.client.HTTPException):
        raw = b""
    finally:
        error.close()
    try:
        code = json.loads(raw).get("error")
    except (ValueError, RecursionError, AttributeError):
        code = None
    if not isinstance(code, str) or not _CODE.match(code):
        code = {401: "unauthorized", 404: "not_found", 429: "rate_limited"}.get(error.code, "bad_answer")
    server_time = None
    date = error.headers.get("Date") if error.headers is not None else None
    if date:
        try:
            server_time = email.utils.parsedate_to_datetime(date).timestamp()
        except (TypeError, ValueError, IndexError):
            server_time = None
    message = _MESSAGES.get(code) or f"The mailbox at {host} refused the request ({error.code} {code})"
    return MailboxError(code, message, error.code, server_time)


def _clock_off(skew: float) -> str:
    minutes = max(1, round(abs(skew) / 60))
    where = "behind" if skew > 0 else "ahead of"
    return (f"This host's clock is about {minutes} minutes {where} the mailbox's, so the mailbox refuses its "
            "requests. Set the clock right (turn on network time) and links work again")


def _canonical_host(host: str) -> Optional[str]:
    # A trailing dot spells the same name from the DNS root; the mailbox's
    # own URL leaves it off.
    host = host.lower().removesuffix(".")
    if not host or not host.isascii():
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        return f"[{address.compressed}]" if address.version == 6 else address.compressed
    # A URL parser reads a host whose last label is a number as an IPv4
    # address in some short form (127.1, 0x7f.1), and signs its own spelling.
    if ":" in host or _NUMERIC_LABEL.match(host.rsplit(".", 1)[-1]):
        return None
    if not re.match(r"^[a-z0-9-]+(?:\.[a-z0-9-]+)*\Z", host):
        return None
    return host


def _on_this_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _host_and_port(scheme: str, host: str, port: Optional[int]) -> str:
    default = 443 if scheme == "https" else 80
    return host if port is None or port == default else f"{host}:{port}"


def _check_sender(sender: Any) -> None:
    if not isinstance(sender, str) or not SENDER_ID.match(sender):
        raise MailboxError("bad_request", "a sender id is 1 to 64 letters, digits, _ or -")


def _whole(value: Any) -> bool:
    return type(value) is int and value >= 0


def _delivered(row: Any, host: str) -> Delivered:
    if not isinstance(row, dict) or not _whole(row.get("seq")) or not isinstance(row.get("sender"), str) \
            or not _whole(row.get("received_at")) or not isinstance(row.get("body"), str):
        raise MailboxError("bad_answer", f"The mailbox at {host} answered a read oddly")
    try:
        body = base64.b64decode(row["body"], validate=True)
    except (binascii.Error, ValueError):
        raise MailboxError("bad_answer", f"The mailbox at {host} answered a read oddly") from None
    return Delivered(seq=row["seq"], sender=row["sender"], received_at=row["received_at"], body=body)
