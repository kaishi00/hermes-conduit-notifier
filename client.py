"""Profile-scoped relay state and non-blocking HTTPS delivery."""

from __future__ import annotations

import json
import logging
import os
import queue
import socket
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

from .events import PLUGIN_VERSION


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


def save_state(value: dict[str, Any]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, separators=(",", ":")) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)
    path.chmod(0o600)


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
        payload=event,
        timeout=timeout,
    )


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
    return request_json(
        f"{state['relay_url'].rstrip('/')}/v1/decisions/{request_id}",
        method="GET",
        credential=state["credential"],
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
