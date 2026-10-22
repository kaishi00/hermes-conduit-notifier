"""Profile-scoped relay state and non-blocking HTTPS delivery."""

from __future__ import annotations

import contextvars
import hashlib
import hmac
import json
import logging
import os
import queue
import secrets
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
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

from hermes_constants import get_hermes_home

from . import e2e
from .events import PLUGIN_VERSION, redact_event
from .relay_urls import check_claim_base, check_request_url, is_https, paired_relay_url


DEFAULT_RELAY_URL = "https://push.milim.dev"
# Derived from PLUGIN_VERSION so a version bump updates the UA automatically
# (no second hand-synchronized constant).
USER_AGENT = f"Hermes-Conduit-Notifier/{PLUGIN_VERSION}"
logger = logging.getLogger("hermes.plugins.conduit_push")
_events: queue.Queue[tuple[contextvars.Context, dict[str, Any]]] = queue.Queue(maxsize=128)
_worker_started = False
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()
# Set while the plugin is unloaded (stop()): nothing more is queued or sent.
# A worker from before a stop() exits even when start() ran meanwhile: it
# belongs to an older generation.
_stopped = threading.Event()
_generation = 0


def state_path() -> Path:
    return get_hermes_home() / "conduit-push.json"


def load_state() -> dict[str, Any] | None:
    path = state_path()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(value, dict) or not value.get("credential"):
        return None
    return value


def state_lock_path(path: Path) -> Path:
    # dashboard/plugin_api.py locks the same file (_pairing_state_lock_path);
    # tests/test_e2e.py checks the two stay identical.
    return path.with_name(f".{path.name}.lock")


@contextmanager
def state_file_lock(path: Path) -> Iterator[None]:
    """Serializes writers of conduit-push.json across processes.

    The hooks run in the agent process and the dashboard provisions the
    encryption key from its own process; both take this lock (the dashboard
    uses the same lock file) around their writes.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(state_lock_path(path), "a+b") as handle:
        with _exclusive(handle):
            yield


@contextmanager
def _exclusive(handle: Any) -> Iterator[None]:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    elif msvcrt is not None:
        # Windows: lock the lock file's first byte. LK_LOCK gives up after
        # about ten seconds; retry for about a minute, then give up loudly.
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


def save_state(value: dict[str, Any]) -> None:
    path = state_path()
    with state_file_lock(path):
        _write_state(value, path)


def update_state(change: Callable[[dict[str, Any]], bool | None]) -> bool:
    """Change the saved pairing in place: read, ``change`` and write it back
    under the state lock, so a pairing another process replaced or removed
    meanwhile is never written over. False when the profile isn't paired or
    ``change`` returned False (nothing written)."""
    path = state_path()
    with state_file_lock(path):
        state = load_state()
        if not state or change(state) is False:
            return False
        _write_state(state, path)
    return True


def _write_state(value: dict[str, Any], path: Path) -> None:
    # Callers hold state_file_lock (a second flock of it from the same
    # process would wait on itself).
    _keep_provisioned_key(value, path)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    # Owner-only from creation: the file holds the relay credential and the
    # pairing secret, so it's never readable by others, even for a moment
    # or if the process dies before the rename.
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(value, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.chmod(0o600)
    temporary.replace(path)
    path.chmod(0o600)


def _keep_provisioned_key(value: dict[str, Any], path: Path) -> None:
    # A caller that loaded the state before the dashboard stored an
    # encryption key must not write it back without the key: that would
    # quietly turn encryption off for the pairing. The key belongs to the
    # pairing, so a new pairing (different installation or gateway) drops it.
    # Likewise a caller holding an older key must not write it back over
    # one the phone has since replaced (keys carry their created_at).
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return
    if (
        not isinstance(current, dict)
        or current.get("e2e") is None
        or current.get("installation_id") != value.get("installation_id")
        or current.get("gateway_id") != value.get("gateway_id")
    ):
        return
    if "e2e" not in value or _key_created_at(current["e2e"]) > _key_created_at(value["e2e"]):
        value["e2e"] = current["e2e"]


def _key_created_at(record: Any) -> str:
    return str(record.get("created_at") or "") if isinstance(record, dict) else ""


def remove_state() -> None:
    path = state_path()
    with state_file_lock(path):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def claim_pairing(code: str, relay_url: str = DEFAULT_RELAY_URL, gateway_name: str = "") -> dict[str, Any]:
    """Claim a pairing code and save the pairing (rules in relay_urls.py).

    A bad relay URL raises RelayURLError (a ValueError) before the code is sent.
    """
    base = check_claim_base(relay_url)
    name = gateway_name.strip() or f"{socket.gethostname()} Hermes"
    body = _request(
        f"{base}/v1/pairings/claim",
        method="POST",
        payload={"pairing_code": code, "gateway_name": name},
        timeout=15.0,
    )
    if not isinstance(body, dict) or not all(isinstance(body.get(key), str) and body[key]
                                             for key in ("credential", "installation_id")):
        raise ValueError("The relay sent an unexpected pairing answer.")
    state = {
        "credential": body["credential"],
        "gateway_id": body.get("gateway_id"),
        "gateway_name": name,
        "installation_id": body["installation_id"],
        "relay_url": paired_relay_url(body.get("relay_url"), base),
    }
    # Re-pairing over an existing pairing must not silently drop the
    # profile's privacy choice, even one switched on meanwhile.
    path = state_path()
    with state_file_lock(path):
        previous = load_state() or {}
        if previous.get("redact_content"):
            state["redact_content"] = True
        if previous.get("redact_key"):
            state["redact_key"] = previous["redact_key"]
        _write_state(state, path)
    _revoke_replaced(previous, state["credential"])
    return state


def _revoke_replaced(previous: dict[str, Any], credential: str) -> None:
    """Best-effort revoke of the credential a re-pair replaced: the relay
    mints a new gateway per claim, so the old one would stay live and take
    one of the phone's gateway slots (#250). The new pairing works either way."""
    old = previous.get("credential")
    if not isinstance(old, str) or not old or old == credential:
        return
    try:
        url = check_request_url(f"{str(previous.get('relay_url') or '').rstrip('/')}/v1/gateways/current")
        request_json(url, method="DELETE", credential=old, timeout=5.0)
    except RelayRejected as error:
        if error.status != 401:
            logger.info("Conduit relay did not revoke the replaced pairing: %s", error)
    except Exception as error:
        logger.info("Conduit relay did not revoke the replaced pairing: %s", type(error).__name__)


class UnpairedLocally(RuntimeError):
    """The pairing was removed here, but its relay URL can't carry the
    credential any more (a pairing saved before HTTPS was required), so the
    relay still holds it until the phone removes this gateway."""


def unpair() -> bool:
    state = load_state()
    if not state:
        remove_state()
        return False
    try:
        url = check_request_url(f"{str(state.get('relay_url') or '').rstrip('/')}/v1/gateways/current")
    except ValueError:
        remove_state()
        raise UnpairedLocally(
            "Removed this profile's pairing, but its saved relay URL is not https://, so the relay was not told. "
            "Remove this gateway in Conduit to revoke it there."
        ) from None
    request_json(url, method="DELETE", credential=state["credential"])
    remove_state()
    return True


def enqueue(event: dict[str, Any]) -> bool:
    """Queue an event for asynchronous relay delivery.

    Returns True when the event was accepted by the local delivery queue and
    False when it was DROPPED (queue full, the profile lost its pairing
    between the caller's check and here, or the relay refused this pairing
    and its notifications are paused). Fire-and-forget callers can ignore
    the result; the clarify middleware MUST NOT — it parks a relay decision
    on this event, so a drop has to fall back to the native clarify path
    instead of polling an answer that can never arrive.
    """
    state = load_state()
    if _stopped.is_set() or not state:
        return False
    if _paused_until(state) > time.monotonic():
        logger.info("Conduit notification dropped for %s: %s", event.get("type", "event"), _pause_reason(state))
        return False
    _start_worker()
    try:
        # Hermes can serve several profiles in one process, each through a
        # context variable for its home that a new thread doesn't inherit.
        # The event goes out under the profile that accepted it here: its
        # pairing, credential and encryption key, not the launch profile's.
        _events.put_nowait((contextvars.copy_context(), event))
    except queue.Full:
        logger.warning("Conduit notification queue is full; dropping %s", event.get("type", "event"))
        return False
    return True


def send_now(event: dict[str, Any], timeout: float = 15.0) -> dict[str, Any]:
    state = load_state()
    if not state:
        raise RuntimeError("This Hermes profile is not paired with Conduit.")
    try:
        answer = _send(event, state, timeout)
    except RelayRejected as error:
        # `test` is a probe too: `status` should say what it just saw. A
        # refusal doesn't start a pause (one person typed it), but an
        # accepted test ends one.
        if error.status in (401, 403):
            record_relay_refusal(state, error)
        raise
    record_relay_accepted(state)
    return answer


def _send(event: dict[str, Any], state: dict[str, Any], timeout: float = 15.0) -> dict[str, Any]:
    return request_json(
        f"{state['relay_url'].rstrip('/')}/v1/events",
        method="POST",
        credential=state["credential"],
        payload=build_outgoing(event, state),
        timeout=timeout,
    )


def set_redact_content(enabled: bool) -> bool:
    """Persist this profile's content-redaction switch (#192).

    Returns False when the profile is not paired (nothing to configure).
    """
    def switch(state: dict[str, Any]) -> None:
        state["redact_content"] = bool(enabled)
        if enabled and not state.get("redact_key"):
            # Local-only key for re-keying event ids; never sent to the relay.
            state["redact_key"] = secrets.token_hex(32)

    return update_state(switch)


def build_outgoing(event: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """The event exactly as it is sent to the relay for this pairing."""
    # Redaction (the user's explicit choice) applies first, then a pairing
    # that provisioned end-to-end encryption seals whatever content is left.
    return _sealed(_redacted(event, state), state)


def _sealed(event: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    # A pairing with a key never sends plaintext content: if sealing fails
    # (no crypto library, a corrupt stored key), the event goes out with its
    # content removed and the phone shows the generic banner. A clarify then
    # parks nothing, so the clarify loop falls back to Hermes' own path.
    # plugin.hello carries no content and keeps its deterministic id, which
    # the relay dedupes across gateways.
    # Presence, not truthiness: an empty or malformed record is a broken key
    # and fails closed like any other.
    if state.get("e2e") is None or event.get("type") == "plugin.hello":
        return event
    keys = None
    try:
        keys = e2e.keys_from_state(state)
        return e2e.seal_event(
            event,
            keys,
            installation_id=str(state.get("installation_id") or ""),
            gateway_id=str(state.get("gateway_id") or ""),
        )
    except Exception as error:
        logger.warning("Conduit notification could not be encrypted; sending it without content: %s", error)
    if keys is None:
        # No usable key to re-key the event id with: use the local-only
        # redaction key so the relay still never sees the plain digest.
        return e2e.content_free(_rekeyed(event, _redact_key(state)))
    return e2e.content_free(event, keys)


def _redacted(event: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    # send_now is the one egress chokepoint (the delivery worker drains
    # enqueue() through it), so redaction runs exactly once per event and
    # every hook and the clarify loop get it without each builder having to
    # remember. The flag is read at send time, so `redact on` covers events
    # already queued and `redact off` releases them unredacted: the switch
    # governs what leaves from the moment it is flipped.
    if not state.get("redact_content"):
        return event
    redacted = redact_event(event)
    # Plain event ids are an unkeyed digest of hook data (for approvals,
    # the command). Re-key them with a local-only secret the relay never
    # receives: it still dedupes replays (same input -> same id) but cannot
    # recompute the digest for guessed commands. Runs once per event at this
    # chokepoint; the output is not meant to be fed back in.
    return _rekeyed(redacted, _redact_key(state))


def _rekeyed(event: dict[str, Any], key: str) -> dict[str, Any]:
    event_id = event.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        return event
    prefix = event_id.split(":", 1)[0] if ":" in event_id else "event"
    keyed = hmac.new(key.encode(), event_id.encode(), hashlib.sha256).hexdigest()[:32]
    return {**event, "event_id": f"{prefix}:{keyed}"}


def _redact_key(state: dict[str, Any]) -> str:
    # A state that has redact_content without a key (hand-edited, or written
    # by a pre-key build) gets one minted and persisted once, so ids stay
    # stable across deliveries and relay dedup keeps working.
    key = state.get("redact_key")
    if not key:
        key = secrets.token_hex(32)

        def keep(current: dict[str, Any]) -> bool:
            nonlocal key
            # Only into the pairing this send read: one replaced or removed
            # since keeps the key for this send alone.
            if any(current.get(name) != state.get(name) for name in ("credential", "installation_id", "gateway_id")):
                return False
            if current.get("redact_key"):  # another process minted one first
                key = current["redact_key"]
                return False
            current["redact_key"] = key
            return True

        update_state(keep)
        state["redact_key"] = key
    return str(key)


def poll_decision(request_id: str) -> dict[str, Any]:
    """Poll the relay for a push-delivered clarify answer by plugin-minted id.

    Returns {"status": "answered", "answer": str} once the device responded
    to a single-question decision, {"status": "answered", "answers": {...},
    "remaining": []} for a completed batch, and {"status": "pending",
    "remaining": [...]} while questions are still open. Raises on transport
    errors so the caller can decide to keep waiting.
    """
    state = load_state()
    if not state:
        raise RuntimeError("This Hermes profile is not paired with Conduit.")
    status = request_json(
        f"{state['relay_url'].rstrip('/')}/v1/decisions/{request_id}",
        method="GET",
        credential=state["credential"],
    )
    return opened_answers(status, state, request_id)


def opened_answers(status: dict[str, Any], state: dict[str, Any], request_id: str) -> dict[str, Any]:
    """Decrypts the answers in a relay poll result for an E2E pairing.

    A pairing that provisioned a key only accepts answers sealed with it and
    bound to this request (and question). Plaintext or anything that fails to
    verify came from somewhere other than the paired phone, so the poll
    reports ``rejected`` and the clarify loop hands the question back to
    Hermes' own clarify path instead of answering with it.
    """
    if state.get("e2e") is None or not isinstance(status, dict):
        return status
    has_answer = "answer" in status
    has_answers = isinstance(status.get("answers"), dict) and bool(status.get("answers"))
    if not has_answer and not has_answers:
        return status
    context = {
        "installation_id": str(state.get("installation_id") or ""),
        "gateway_id": str(state.get("gateway_id") or ""),
        "request_id": request_id,
    }
    try:
        keys = e2e.keys_from_state(state)
        opened = dict(status)
        if has_answer:
            opened["answer"] = e2e.open_answer(status.get("answer"), keys, question_id="", **context)
        if has_answers:
            opened["answers"] = {
                str(qid): e2e.open_answer(value, keys, question_id=str(qid), **context)
                for qid, value in status["answers"].items()
            }
        return opened
    except Exception as error:
        logger.warning("Conduit rejected an answer for %s that was not sealed by the paired phone: %s", request_id, error)
        return {"status": "rejected"}


def e2e_status(state: dict[str, Any] | None = None) -> dict[str, Any]:
    """This profile's end-to-end encryption state, without the secret."""
    state = load_state() if state is None else state
    record = (state or {}).get("e2e")
    try:
        usable = e2e.keys_from_state(state or {}) is not None
    except Exception:
        usable = False
    return {
        # On only with a key that can actually seal; a broken one sends
        # content-free events and reads as off here.
        "enabled": usable,
        "kid": record.get("kid") if isinstance(record, dict) else None,
        "crypto": e2e.available(),
    }


def post_event(payload: dict[str, Any], timeout: float = 15.0) -> dict[str, Any]:
    """POST an already-built outgoing event (diagnostics)."""
    state = load_state()
    if not state:
        raise RuntimeError("This Hermes profile is not paired with Conduit.")
    return request_json(
        f"{state['relay_url'].rstrip('/')}/v1/events",
        method="POST",
        credential=state["credential"],
        payload=payload,
        timeout=timeout,
    )


def cancel_decision(request_id: str) -> bool:
    """Release a parked decision the relay loop can no longer complete.

    Called when the ORIGINAL clarify path won the race (desktop/CLI answered
    through the gateway) or the poll budget fell back to it: without this, a
    device answering the stale card would get a 200 "answered" from the relay
    while the tool result is silently discarded — the card would claim an
    answer Hermes never received. Best effort by contract: a relay without
    the endpoint (or a transport blip) must never break the answering path.
    """
    try:
        state = load_state()
        if not state:
            return False
        request_json(
            f"{state['relay_url'].rstrip('/')}/v1/decisions/{request_id}",
            method="DELETE",
            credential=state["credential"],
        )
        return True
    except Exception as error:
        logger.warning("Conduit decision cancel failed for %s: %s", request_id, error)
        return False


def looks_like_json_object(raw: bytes) -> bool:
    """A body that starts as a JSON object (whitespace and a BOM aside), cut
    short or not: the relay's answer rather than a firewall's."""
    return raw.lstrip().removeprefix(b"\xef\xbb\xbf").lstrip().startswith(b"{")


class RelayRejected(RuntimeError):
    """The relay answered with an HTTP error or a redirect (never followed).
    ``status`` tells a redirect (3xx) or refusal (4xx) from the relay failing
    to handle the request (5xx). ``from_relay`` is False when the answer had
    no JSON error body: the relay always sends one, so something in front of
    it (a firewall, a rate limiter, a reverse proxy) answered instead."""

    def __init__(self, status: int, detail: str, *, from_relay: bool = True) -> None:
        # Safe to log verbatim: one printable line of 200 characters at most,
        # whatever the relay sent.
        detail = " ".join("".join(ch if ch.isprintable() else " " for ch in str(detail)).split())[:200]
        detail = detail or "request_rejected"
        super().__init__(f"Conduit relay rejected the request: {detail} ({status})." if from_relay
                         else f"Something in front of the Conduit relay refused the request ({status}).")
        self.status = status
        self.detail = detail
        self.from_relay = from_relay

    @property
    def edge_blocked(self) -> bool:
        """A bare 403 from in front of the relay: this host's address is
        blocked there. Pairing again can't help, it meets the same block."""
        return self.status == 403 and not self.from_relay


def request_json(
    url: str,
    *,
    method: str,
    payload: dict[str, Any] | None = None,
    credential: str = "",
    timeout: float = 15.0,
) -> dict[str, Any]:
    """One relay request. ``timeout`` bounds connect and read, but urllib
    cannot bound a pathological DNS resolution — accepted technical debt;
    bounding it would mean a resolver redesign for no realistic gain.

    The URL must be HTTPS (plain HTTP only to this machine), and redirects
    are refused: urllib would resend the Authorization header to wherever
    one points. A refused redirect raises RelayRejected with its 3xx status.
    """
    check_request_url(url)
    return _request(url, method=method, payload=payload, credential=credential, timeout=timeout)


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_opener = urllib.request.build_opener(_RefuseRedirects)
# Plain HTTP (loopback, or a LAN/Tailscale pairing claim) never goes through
# an HTTP(S)_PROXY, which would see the credential or pairing code in clear.
_direct_opener = urllib.request.build_opener(_RefuseRedirects, urllib.request.ProxyHandler({}))
MAX_RESPONSE_BYTES = 1024 * 1024


def _request(
    url: str,
    *,
    method: str,
    payload: dict[str, Any] | None = None,
    credential: str = "",
    timeout: float,
) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    if credential:
        headers["Authorization"] = f"Bearer {credential}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        opener = _opener if is_https(url) else _direct_opener
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise RelayRejected(response.status, "response_too_large")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        try:
            raw = error.read(4096)
        except Exception:
            raw = None
        try:
            body = json.loads(raw) if raw else None
        except Exception:
            body = None
        # The relay always answers with a JSON object. An empty or non-JSON
        # body is something in front of it; one cut short or unreadable is
        # still counted as the relay's, so a misread never says "blocked".
        from_relay = raw is None or looks_like_json_object(raw)
        detail = (body.get("error") if isinstance(body, dict) else None) or "request_rejected"
        raise RelayRejected(error.code, detail, from_relay=from_relay) from error


def _start_worker() -> None:
    global _worker_started, _worker
    if _worker_started:
        return
    with _worker_lock:
        if _worker_started or _stopped.is_set():
            return
        _worker = threading.Thread(target=_delivery_worker, args=(_generation,), name="conduit-push", daemon=True)
        _worker.start()
        _worker_started = True


def start() -> None:
    """Lets events be queued again after stop() (the plugin was enabled
    again without a fresh import)."""
    _clear_refusal()
    _stopped.clear()


def stop(timeout: float = 1.0) -> None:
    """The plugin is being unloaded: events still queued are dropped, the
    worker exits after the send in progress (its retries end), and later
    enqueue() calls are refused. Waits up to ``timeout`` for the worker."""
    global _worker_started, _worker, _generation
    with _worker_lock:
        _stopped.set()
        _generation += 1
        # A reload (after pairing again, say) starts unpaused.
        _clear_refusal()
        running, _worker, _worker_started = _worker, None, False
        dropped = 0
        while True:
            try:
                _events.get_nowait()
            except queue.Empty:
                break
            _events.task_done()
            dropped += 1
        if running is not None:
            _events.put_nowait(None)  # wakes the worker; the queue was just emptied
    if dropped:
        logger.info("Conduit dropped %d queued notification(s): the plugin was unloaded", dropped)
    if running is not None:
        running.join(timeout)


# A delivery that fails on the way (no connection, a timeout, the relay busy
# or failing) is sent again. That's safe: the relay keeps each accepted
# event_id per pairing and answers a repeat as a duplicate, and an event's id
# stays the same from one send to the next (redaction and encryption re-key it
# the same way every time). One thread delivers everything, so the retries of
# one event stop after RETRY_BUDGET seconds instead of holding up the rest.
RETRY_DELAYS = (1.0, 2.0, 4.0)
RETRY_BUDGET = 30.0

# A 401 or 403 is the relay (or something in front of it) refusing this host,
# and the next event would be refused the same way. Notifications are parked
# for the pairing instead: 1 minute after the first refusal, doubling with
# each refusal in a row up to 30 minutes, and events queued meanwhile are
# dropped. The first event after the pause goes out as the probe; an accepted
# one ends it. Each pairing (credential) has its own pause: profiles served
# by one process don't share one, and pairing again isn't held by the old one.
REFUSED_FIRST_S = 60.0
REFUSED_MAX_S = 30 * 60.0
_refusals: dict[str, dict[str, Any]] = {}
_refusal_lock = threading.Lock()


def _paused_until(state: dict[str, Any]) -> float:
    """When this pairing's refusal pause ends (0 when it has none)."""
    with _refusal_lock:
        return _refusals.get(str(state.get("credential")), {}).get("until", 0.0)


def notifications_paused() -> bool:
    """Whether this profile's pairing is in a refusal pause: its events are
    dropped, and nothing should poll the relay for it meanwhile."""
    state = load_state()
    return bool(state) and _paused_until(state) > time.monotonic()


def _pause_reason(state: dict[str, Any]) -> str:
    with _refusal_lock:
        blocked = _refusals.get(str(state.get("credential")), {}).get("edge_blocked")
    return ("something in front of the relay is blocking this host, notifications are paused" if blocked
            else "the relay refused this pairing, notifications are paused")


def _clear_refusal(credential: Any = None) -> None:
    """Ends every pause, or only the one held for ``credential``."""
    with _refusal_lock:
        if credential is None:
            _refusals.clear()
        else:
            _refusals.pop(str(credential), None)


def _delivery_worker(generation: int = 0) -> None:
    while generation == _generation:
        item = _events.get()
        try:
            if item is None:  # stop()'s wake-up
                continue
            if generation != _generation:
                # Queued for the worker started after a stop(): hand it back.
                _requeue(item)
                continue
            context, event = item
            context.run(_deliver, event)
        except Exception as error:  # the worker thread must outlive any one event
            logger.warning("Conduit notification delivery failed for %s: %s", event.get("type", "event"), error)
        finally:
            _events.task_done()


def _requeue(item: Any) -> None:
    try:
        _events.put_nowait(item)
    except queue.Full:
        logger.warning("Conduit notification queue is full; dropping %s", item[1].get("type", "event"))


def _pause(seconds: float) -> None:
    # A stop() ends the wait (and _deliver then gives up).
    _stopped.wait(seconds)


def _deliver(event: dict[str, Any], *, sleep: Callable[[float], None] = _pause,
             clock: Callable[[], float] = time.monotonic) -> bool:
    started, generation = clock(), _generation
    for delay in (*RETRY_DELAYS, None):
        if _stopped.is_set():
            logger.info("Conduit notification dropped for %s: the plugin was unloaded", event.get("type", "event"))
            return False
        state = load_state()
        if not state:
            logger.warning("Conduit notification dropped for %s: this profile is no longer paired", event.get("type", "event"))
            return False
        paused_until, now = _paused_until(state), clock()
        if now < paused_until:
            logger.info("Conduit notification dropped for %s: %s for %d more seconds",
                        event.get("type", "event"), _pause_reason(state), paused_until - now)
            return False
        try:
            _send(event, state)
        except Exception as error:
            if delay is not None and _worth_retrying(error) and clock() - started + delay <= RETRY_BUDGET:
                logger.info("Conduit notification delivery for %s will be retried: %s", event.get("type", "event"), error)
                sleep(delay)
                continue
            logger.warning("Conduit notification delivery failed for %s: %s", event.get("type", "event"), error)
            if isinstance(error, RelayRejected) and error.status in (401, 403):
                _refused(state, error, clock(), generation)
            return False
        record_relay_accepted(state)
        return True
    return False  # not reached: the last attempt has no delay


def _worth_retrying(error: Exception) -> bool:
    if isinstance(error, RelayRejected):
        # 408/429 and the relay failing; any other answer would be the same.
        return error.status in (408, 429) or 500 <= error.status <= 599
    if isinstance(error, urllib.error.URLError):
        # No connection, DNS, a reset: worth another go. A certificate the
        # relay's name doesn't match won't fix itself in seconds.
        return not isinstance(error.reason, ssl.SSLCertVerificationError)
    return isinstance(error, (TimeoutError, ConnectionError))


def _refused(state: dict[str, Any], error: RelayRejected, now: float, generation: int) -> None:
    with _refusal_lock:
        # A refusal in flight across a stop() mustn't pause the reload.
        if generation != _generation:
            return
        previous = _refusals.get(str(state.get("credential")), {}).get("paused_for")
        paused_for = min(previous * 2, REFUSED_MAX_S) if previous else REFUSED_FIRST_S
        _refusals[str(state.get("credential"))] = {"paused_for": paused_for, "until": now + paused_for,
                                                   "edge_blocked": error.edge_blocked}
    logger.warning("Conduit notifications paused for %d minute(s): %s", paused_for // 60,
                   "something in front of the relay is blocking this host (an empty 403)" if error.edge_blocked
                   else f"the relay answered {error.status}")
    record_relay_refusal(state, error)


def record_relay_refusal(sent_with: dict[str, Any], error: RelayRejected) -> None:
    """What `status` (and Conduit) should say about a refusal: a 401 means the
    relay no longer knows this gateway, a bare 403 that this host is blocked
    in front of the relay. A relay's own 403 is neither."""
    # The newest answer stands: one kind of refusal replaces the other. A 401
    # without the relay's JSON came from in front of it: it says nothing
    # about the pairing.
    if error.status == 401 and error.from_relay:
        _mark_relay_rejected(sent_with, True, replaces="relay_blocked_at")
    elif error.edge_blocked:
        _mark_relay_rejected(sent_with, True, field="relay_blocked_at", replaces="relay_rejected_at")


def record_relay_accepted(sent_with: dict[str, Any]) -> None:
    """The relay took a request with this pairing: its notification pause
    ends, and neither mark holds."""
    _clear_refusal(sent_with.get("credential"))
    for field in ("relay_rejected_at", "relay_blocked_at"):
        if sent_with.get(field):
            _mark_relay_rejected(sent_with, False, field=field)


def _mark_relay_rejected(sent_with: dict[str, Any], rejected: bool, *, field: str = "relay_rejected_at",
                         replaces: str | None = None) -> None:
    """Records that the relay refused this pairing's credential (401), or that
    it accepts it again. A 401 means the relay no longer knows the gateway:
    the phone removed it, or the relay lost its data. The pairing is kept
    (it holds the redaction key, and the relay may be restored), but
    `status` tells the user to pair again. ``field`` "relay_blocked_at" marks
    a bare 403 from in front of the relay the same way, which `status` says
    pairing again won't fix; ``replaces`` names the other mark, dropped in
    the same write. Only the pairing that was used is marked: one replaced or
    removed meanwhile is left alone."""

    def mark(current: dict[str, Any]) -> bool:
        if any(current.get(name) != sent_with.get(name) for name in ("credential", "installation_id", "gateway_id")):
            return False
        if rejected:
            replaced = replaces is not None and current.pop(replaces, None) is not None
            if current.get(field):
                return replaced
            current[field] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            return True
        return current.pop(field, None) is not None

    try:
        update_state(mark)
    except OSError as error:
        logger.warning("Conduit could not record the relay's answer for this pairing: %s", error)
