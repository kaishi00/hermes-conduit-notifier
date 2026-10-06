"""Profile-scoped relay state and non-blocking HTTPS delivery."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import queue
import secrets
import socket
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # Windows: no advisory locks; writes stay atomic.
    fcntl = None  # type: ignore[assignment]

from hermes_constants import get_hermes_home

from . import e2e
from .events import PLUGIN_VERSION, redact_event


DEFAULT_RELAY_URL = "https://push.milim.dev"
# Derived from PLUGIN_VERSION so a version bump updates the UA automatically
# (no second hand-synchronized constant).
USER_AGENT = f"Hermes-Conduit-Notifier/{PLUGIN_VERSION}"
logger = logging.getLogger("hermes.plugins.conduit_push")
_events: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=128)
_worker_started = False
_worker_lock = threading.Lock()


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
    if fcntl is None:
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(state_lock_path(path), "a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def save_state(value: dict[str, Any]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with state_file_lock(path):
        _keep_provisioned_key(value, path)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        temporary.write_text(json.dumps(value, separators=(",", ":")) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)
        path.chmod(0o600)


def _keep_provisioned_key(value: dict[str, Any], path: Path) -> None:
    # A caller that loaded the state before the dashboard stored an
    # encryption key must not write it back without the key: that would
    # quietly turn encryption off for the pairing. The key belongs to the
    # pairing, so a new pairing (different installation or gateway) drops it.
    if "e2e" in value:
        return
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return
    if (
        isinstance(current, dict)
        and current.get("e2e") is not None
        and current.get("installation_id") == value.get("installation_id")
        and current.get("gateway_id") == value.get("gateway_id")
    ):
        value["e2e"] = current["e2e"]


def remove_state() -> None:
    try:
        state_path().unlink()
    except FileNotFoundError:
        pass


def claim_pairing(code: str, relay_url: str = DEFAULT_RELAY_URL, gateway_name: str = "") -> dict[str, Any]:
    name = gateway_name.strip() or f"{socket.gethostname()} Hermes"
    body = request_json(
        f"{relay_url.rstrip('/')}/v1/pairings/claim",
        method="POST",
        payload={"pairing_code": code, "gateway_name": name},
    )
    state = {
        "credential": body["credential"],
        "gateway_id": body.get("gateway_id"),
        "gateway_name": name,
        "installation_id": body["installation_id"],
        "relay_url": body.get("relay_url") or relay_url.rstrip("/"),
    }
    # Re-pairing over an existing pairing must not silently drop the
    # profile's privacy choice.
    previous = load_state() or {}
    if previous.get("redact_content"):
        state["redact_content"] = True
    if previous.get("redact_key"):
        state["redact_key"] = previous["redact_key"]
    save_state(state)
    return state


def unpair() -> bool:
    state = load_state()
    if not state:
        remove_state()
        return False
    request_json(
        f"{state['relay_url'].rstrip('/')}/v1/gateways/current",
        method="DELETE",
        credential=state["credential"],
    )
    remove_state()
    return True


def enqueue(event: dict[str, Any]) -> bool:
    """Queue an event for asynchronous relay delivery.

    Returns True when the event was accepted by the local delivery queue and
    False when it was DROPPED (queue full, or the profile lost its pairing
    between the caller's check and here). Fire-and-forget callers can ignore
    the result; the clarify middleware MUST NOT — it parks a relay decision
    on this event, so a drop has to fall back to the native clarify path
    instead of polling an answer that can never arrive.
    """
    if not load_state():
        return False
    _start_worker()
    try:
        _events.put_nowait(event)
    except queue.Full:
        logger.warning("Conduit notification queue is full; dropping %s", event.get("type", "event"))
        return False
    return True


def send_now(event: dict[str, Any], timeout: float = 15.0) -> dict[str, Any]:
    state = load_state()
    if not state:
        raise RuntimeError("This Hermes profile is not paired with Conduit.")
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
    state = load_state()
    if not state:
        return False
    state["redact_content"] = bool(enabled)
    if enabled and not state.get("redact_key"):
        # Local-only key for re-keying event ids; never sent to the relay.
        state["redact_key"] = secrets.token_hex(32)
    save_state(state)
    return True


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
        state["redact_key"] = key
        save_state(state)
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
    return {
        "enabled": isinstance(record, dict) and e2e.valid_kid(record.get("kid")),
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
    bounding it would mean a resolver redesign for no realistic gain."""
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
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read()).get("error", "request_rejected")
        except Exception:
            detail = "request_rejected"
        raise RuntimeError(f"Conduit relay rejected the request: {detail} ({error.code}).") from error


def _start_worker() -> None:
    global _worker_started
    if _worker_started:
        return
    with _worker_lock:
        if _worker_started:
            return
        threading.Thread(target=_delivery_worker, name="conduit-push", daemon=True).start()
        _worker_started = True


def _delivery_worker() -> None:
    while True:
        event = _events.get()
        try:
            send_now(event)
        except Exception as error:
            logger.warning("Conduit notification delivery failed for %s: %s", event.get("type", "event"), error)
        finally:
            _events.task_done()
