"""End-to-end encrypted notifications and pairing from Conduit."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import importlib.util
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
import contextlib
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from fastapi import HTTPException, Request, Response

from . import capabilities, core, watch_tools

logger = core.logger
router = core.router


# --- End-to-end encrypted notifications (#431) -------------------------------
#
# Conduit creates a per-pairing secret on the phone and hands it to this
# profile here, over the dashboard connection, so it never passes through the
# push relay. The hooks (client.py) read it from the profile's
# conduit-push.json, seal every notification with it and only accept clarify
# answers sealed by the phone. GET never returns the secret.

E2E_LIMIT = 10
E2E_WINDOW_S = 60.0
E2E_MAX_BODY_BYTES = 4 * 1024
_E2E_KID = re.compile(r"^[0-9a-f]{32}\Z")
_E2E_B64URL = re.compile(r"^[A-Za-z0-9_-]{43}\Z")  # 32 bytes, unpadded
# Same as e2e.KEY_MAX_AGE_S and e2e.PREVIOUS_KEY_GRACE_S (tests/test_e2e.py
# checks): the phone rotates a key this old, and the key a rotation replaced
# opens answers this long.
E2E_KEY_MAX_AGE_S = 7 * 24 * 3600
E2E_PREVIOUS_KEY_GRACE_S = 7 * 24 * 3600
# Earlier rotation attempts sealed with a replaced key that are remembered
# (as digests) and refused if they arrive again.
E2E_ROTATE_RETRIES_KEPT = 16
_E2E_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_e2e_limiter = core._MintLimiter(E2E_LIMIT, E2E_WINDOW_S, message="Too many encryption key requests; try again shortly")
_e2e_lock = threading.Lock()


def _pairing_state_lock_path(path: Any) -> Any:
    return path.with_name(f".{path.name}.lock")


@contextlib.contextmanager
def _pairing_state_lock(path: Any):
    # The same lock file client.state_file_lock takes in the agent process,
    # so a hook rewriting the state can't drop a key stored here (or the
    # other way round).
    with open(_pairing_state_lock_path(path), "a+b") as handle, _locked_handle(handle):
        yield


def _open_private_lock_file(lock_path: str) -> Any:
    """A lock file opened owner-only and never through a symlink."""
    flags = os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(lock_path, flags, 0o600)
    try:
        return os.fdopen(fd, "a+b")
    except BaseException:
        os.close(fd)
        raise


@contextlib.contextmanager
def _locked_handle(handle: Any):
    """flock on ``handle``, or a first-byte lock on Windows."""
    try:
        import fcntl
    except ImportError:
        fcntl = None
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    try:
        import msvcrt
    except ImportError:
        yield
        return
    # Windows: the same first-byte lock client.state_file_lock takes.
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


def _save_pairing_state(path: Any, value: Dict[str, Any]) -> None:
    # Same atomic, owner-only write as client.save_state.
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


def _e2e_crypto_available() -> bool:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305  # noqa: F401
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF  # noqa: F401
    except Exception:
        return False
    return True


def _e2e_time(value: Any) -> Optional[float]:
    try:
        return datetime.strptime(str(value), _E2E_TIME_FORMAT).replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def _e2e_live_previous(state: Dict[str, Any], now: float) -> Optional[Dict[str, Any]]:
    """The key the last rotation replaced, while it still opens answers."""
    record = state.get("e2e_previous")
    if not isinstance(record, dict) or not isinstance(record.get("kid"), str) or not _E2E_KID.match(record["kid"]):
        return None
    retired = _e2e_time(record.get("retired_at"))
    if retired is None or now >= retired + E2E_PREVIOUS_KEY_GRACE_S:
        return None
    return record


def _e2e_prune_previous(path: Any, now: float) -> None:
    """Deletes a replaced key once its overlap is over, so its secret doesn't
    sit on disk (and in backups) until the next rotation. Best effort: a
    status read never fails on it, and an expired key opens nothing anyway."""
    try:
        with _e2e_lock, _pairing_state_lock(path):
            state = core._load_pairing_state(path)
            if state and "e2e_previous" in state and _e2e_live_previous(state, now) is None:
                state.pop("e2e_previous", None)
                _save_pairing_state(path, state)
    except OSError:
        pass


def e2e_status(path: Any = None) -> Dict[str, Any]:
    """This profile's pairing and key state. Never includes the secret.

    ``rotate`` asks the phone for a new key: requested from the host
    (`hermes conduit-push e2e rotate`), or the key is past
    E2E_KEY_MAX_AGE_S. A phone that sees the field knows this plugin keeps
    the replaced key for answers, so rotating loses nothing, unless
    ``rotate_without_overlap`` says the host asked to drop it (a key that may
    have leaked)."""
    path = path if path is not None else core._pairing_state_path()
    state = core._load_pairing_state(path)
    crypto = _e2e_crypto_available()
    if not state:
        return {"paired": False, "crypto": crypto}
    record = state.get("e2e")
    kid = record.get("kid") if isinstance(record, dict) else None
    secret = record.get("secret") if isinstance(record, dict) else None
    # A key whose secret is unusable reads as none, so the phone replaces it.
    if not isinstance(secret, str) or not _E2E_B64URL.match(secret):
        kid = None
    usable = isinstance(kid, str) and bool(_E2E_KID.match(kid))
    now = time.time()
    created = _e2e_time(record.get("created_at")) if usable else None
    previous = _e2e_live_previous(state, now) if usable else None
    if state.get("e2e_previous") is not None and previous is None:
        _e2e_prune_previous(path, now)
    return {
        "paired": True,
        "installation_id": state.get("installation_id"),
        "gateway_id": state.get("gateway_id"),
        "e2e": {"kid": kid, "created_at": record.get("created_at"), "suite": record.get("suite") or "handover"} if usable else None,
        # Inline rotation (POST /e2e/rotate) and its key exchanges, best
        # first; the phone picks the best one it can run.
        "rotate_suites": _e2e_rotate_suites() if usable else [],
        "previous_kid": previous["kid"] if previous else None,
        "rotate": usable and (bool(state.get("rotate_requested_at")) or created is None
                              or now >= created + E2E_KEY_MAX_AGE_S),
        "rotate_without_overlap": usable and bool(state.get("rotate_without_overlap")),
        "crypto": crypto,
        # The relay answered 401 to this pairing's last delivery (client.py):
        # it no longer knows this gateway, so the phone should pair again.
        "relay_rejected": bool(state.get("relay_rejected_at")),
        # Something in front of the relay answered a bare 403 (client.py):
        # this host is blocked there, and pairing again won't get through.
        "relay_blocked": bool(state.get("relay_blocked_at")),
    }


def provision_e2e(body: Any, path: Any = None) -> Dict[str, Any]:
    """Stores the phone's pairing secret for this profile.

    Only for the pairing the phone owns: the installation and gateway ids
    must match this profile's pairing, so a key for another phone (or an old
    pairing) is refused. A new key for the same pairing is a rotation: the
    key it replaces still opens answers for E2E_PREVIOUS_KEY_GRACE_S (unless
    the host asked to rotate without overlap), and pushes use the new one.
    Only the last replaced key is kept: a second rotation inside the overlap
    retires the one before it at once. Keys rotate days apart, and answers
    are sealed seconds after the push they answer, so none is lost in
    practice. While its overlap lasts, the replaced key can't come back: a
    delayed retry of its handover must not roll the pairing back. After that
    (or after a rotation without overlap, or a second one inside it) the host
    no longer knows the key; the phone never sends a key it retired, and
    every POST already needs the dashboard sign-in, so no tombstones are kept.
    """
    if not isinstance(body, dict):
        raise core.TokenError(400, "Expected a JSON object")
    installation_id = body.get("installation_id")
    gateway_id = body.get("gateway_id")
    kid = body.get("kid")
    secret = body.get("secret")
    if not isinstance(installation_id, str) or not installation_id or not isinstance(gateway_id, str) or not gateway_id:
        raise core.TokenError(400, "installation_id and gateway_id are required")
    if not isinstance(kid, str) or not _E2E_KID.match(kid):
        raise core.TokenError(400, "kid must be 32 lowercase hex characters")
    if not isinstance(secret, str) or not _E2E_B64URL.match(secret):
        raise core.TokenError(400, "secret must be 32 bytes, base64url without padding")
    if not _e2e_crypto_available():
        raise core.TokenError(501, "Encrypted notifications need the cryptography package on this host")
    path = path if path is not None else core._pairing_state_path()
    with _e2e_lock, _pairing_state_lock(path):
        state = core._load_pairing_state(path)
        if not state:
            raise core.TokenError(409, "This Hermes profile isn't paired with Conduit")
        if not state.get("gateway_id"):
            raise core.TokenError(409, "Pair this Hermes profile with Conduit again to turn on encryption")
        if state.get("installation_id") != installation_id or state.get("gateway_id") != gateway_id:
            raise core.TokenError(409, "This Hermes profile is paired with a different device")
        now = datetime.now(timezone.utc)
        current = state.get("e2e")
        if isinstance(current, dict) and current.get("kid") == kid:
            if isinstance(current.get("secret"), str) and hmac.compare_digest(current["secret"], secret):
                return {"kid": kid}  # a retry of a key already stored
            # Key ids are random per key: the same id with another secret
            # would make answers ambiguous during the overlap.
            raise core.TokenError(409, "This key id is already in use with a different secret")
        previous_record = state.get("e2e_previous")
        if isinstance(previous_record, dict) and previous_record.get("kid") == kid:
            raise core.TokenError(409, "This key was replaced; send a new one")
        rotated = isinstance(current, dict) and isinstance(current.get("kid"), str) and current["kid"] != kid \
            and isinstance(current.get("secret"), str) and _E2E_B64URL.match(current["secret"])
        if rotated and not state.get("rotate_without_overlap"):
            state["e2e_previous"] = {
                "kid": current["kid"],
                "secret": current["secret"],
                "retired_at": now.strftime(_E2E_TIME_FORMAT),
            }
        else:
            state.pop("e2e_previous", None)
        state["e2e"] = {
            "kid": kid,
            "secret": secret,
            "created_at": now.strftime(_E2E_TIME_FORMAT),
        }
        state.pop("rotate_requested_at", None)
        state.pop("rotate_without_overlap", None)
        _save_pairing_state(path, state)
    return {"kid": kid}


_e2e_module_lock = threading.Lock()
_e2e_module_value: Any = None
_e2e_suites_value: Optional[List[str]] = None


def _e2e_module() -> Any:
    """e2e.py in the plugin's folder, loaded once: the rotation's key exchange."""
    global _e2e_module_value
    with _e2e_module_lock:
        if _e2e_module_value is None:
            path = os.path.join(core._plugin_root(), "e2e.py")
            spec = importlib.util.spec_from_file_location("conduit_push_dashboard_e2e", path)
            if spec is None or spec.loader is None:
                raise core.TokenError(501, "This plugin install is missing e2e.py; update the plugin")
            module = importlib.util.module_from_spec(spec)
            # dataclasses look the module up while the class is built.
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            _e2e_module_value = module
        return _e2e_module_value


def _e2e_rotate_suites() -> List[str]:
    """The suites this host can run. Only a successful probe is cached: a
    transient failure (e2e.py unreadable mid-update) is tried again on the
    next (rate-limited) request instead of turning inline rotation off until
    a restart."""
    global _e2e_suites_value
    if _e2e_suites_value is None:
        try:
            _e2e_suites_value = list(_e2e_module().rotate_suites())
        except Exception:
            return []
    return _e2e_suites_value


def _e2e_rotate_request_digest(body: Dict[str, Any]) -> str:
    """Fingerprint of one rotation request: its key ids, suite and sealed
    content (which carries kid_new and the phone's fresh shares). A resend of
    the same request matches; a new attempt sealed with the same key doesn't."""
    material = json.dumps([body.get("kid"), body.get("suite"), body.get("sealed")],
                          sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def rotate_e2e(body: Any, path: Any = None) -> Dict[str, Any]:
    """Inline rotation: the phone's request, sealed with the current key,
    carries its half of a key exchange (X25519, and ML-KEM-768 on iOS 26+);
    the new secret mixes the exchange with the old secret, so it never
    crosses the dashboard connection. The old key stays as e2e_previous for
    E2E_PREVIOUS_KEY_GRACE_S.

    A resend of the request that made the current key (same kid, suite and
    sealed content) gets the same reply, so a lost reply loses nothing. A
    different request sealed with that replaced key (a phone that lost its
    pending exchange, e.g. after a restart, and starts over) is answered as a
    fresh rotation from the replaced key: the key it never received is
    dropped. During its overlap the replaced key can thus roll the pairing
    forward, which adds nothing over dashboard access (POST /e2e can
    replace the key too). An earlier attempt that was already answered (the last
    E2E_ROTATE_RETRIES_KEPT), and any other request sealed with a replaced
    key, is refused with 409.

    A rotation without overlap (`rotate --now`) keeps nothing to resend: its
    retry is refused with 409 and the phone hands over a new key."""
    if not isinstance(body, dict):
        raise core.TokenError(400, "Expected a JSON object")
    installation_id, gateway_id = body.get("installation_id"), body.get("gateway_id")
    if not isinstance(installation_id, str) or not installation_id or not isinstance(gateway_id, str) or not gateway_id:
        raise core.TokenError(400, "installation_id and gateway_id are required")
    if body.get("suite") not in _e2e_rotate_suites():
        raise core.TokenError(400, "This host can't run that rotation suite")
    module = _e2e_module()
    path = path if path is not None else core._pairing_state_path()
    with _e2e_lock, _pairing_state_lock(path):
        state = core._load_pairing_state(path)
        if not state:
            raise core.TokenError(409, "This Hermes profile isn't paired with Conduit")
        if state.get("installation_id") != installation_id or state.get("gateway_id") != gateway_id:
            raise core.TokenError(409, "This Hermes profile is paired with a different device")
        current, previous = state.get("e2e"), state.get("e2e_previous")
        if not isinstance(current, dict):
            raise core.TokenError(409, "This pairing has no key to rotate yet")
        digest = _e2e_rotate_request_digest(body)
        # The key being rotated: the current one, or (for a phone that never
        # got the reply of the rotation that replaced it) the replaced one.
        base = current
        if isinstance(previous, dict) and isinstance(body.get("kid"), str) and previous.get("kid") == body["kid"]:
            reply = previous.get("rotate_reply")
            made_current = isinstance(reply, dict) and reply.get("kid_new") == current.get("kid")
            # A digest of public request fields, not a secret: a plain
            # compare also survives a hand-edited, non-ASCII state file.
            if made_current and previous.get("rotate_request") == digest:
                return {"rotation": reply}  # the phone missed our answer
            if not made_current or _e2e_live_previous(state, time.time()) is None:
                raise core.TokenError(409, "This key was replaced; send a new one")
            answered = previous.get("rotate_requests_before")
            if isinstance(answered, list) and digest in answered:
                # An earlier attempt, delivered late or replayed: rotating
                # again would drop the key the phone took since.
                raise core.TokenError(409, "This rotation request was already answered")
            # Same old key, another request: the phone started over without
            # the key we answered with, so it never holds it.
            base = previous
        try:
            keys = module.derive_keys(str(base.get("kid") or ""), module.unb64u(str(base.get("secret") or "")))
            rotation = module.answer_rotation(body, keys, module.unb64u(str(base.get("secret") or "")),
                                              installation_id=installation_id, gateway_id=gateway_id)
        except module.E2EError as exc:
            raise core.TokenError(409, f"The rotation request was refused: {exc}")
        if rotation.kid_new in (current.get("kid"), previous.get("kid") if isinstance(previous, dict) else None):
            # The new key id must name one secret only, while either key
            # still opens answers.
            raise core.TokenError(409, "This key id is already in use with a different secret")
        now = datetime.now(timezone.utc).strftime(_E2E_TIME_FORMAT)
        if state.get("rotate_without_overlap"):
            state.pop("e2e_previous", None)
        else:
            # Starting over from the replaced key keeps its first retirement
            # time, so retries never stretch its overlap.
            retired_at = base.get("retired_at") if base is previous else now
            record = {"kid": base["kid"], "secret": base["secret"], "retired_at": retired_at or now,
                      "rotate_reply": rotation.reply, "rotate_request": digest}
            if base is previous:
                before = previous.get("rotate_requests_before")
                before = [d for d in before if isinstance(d, str)] if isinstance(before, list) else []
                if isinstance(previous.get("rotate_request"), str):
                    before.append(previous["rotate_request"])
                record["rotate_requests_before"] = before[-E2E_ROTATE_RETRIES_KEPT:]
            state["e2e_previous"] = record
        state["e2e"] = {"kid": rotation.kid_new, "secret": module.b64u(rotation.secret), "created_at": now,
                        "suite": rotation.suite}
        state.pop("rotate_requested_at", None)
        state.pop("rotate_without_overlap", None)
        _save_pairing_state(path, state)
    return {"rotation": rotation.reply}


@router.post("/e2e/rotate")
async def post_e2e_rotate(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        await core._scoped_acquire(_e2e_limiter, profile)
        body = await core._read_json_body(request, E2E_MAX_BODY_BYTES)
        return {"ok": True, **(await core._run_scoped(profile, lambda: rotate_e2e(body)))}
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise core._unexpected("rotation", exc, feature="Encrypted notifications")


@router.get("/e2e")
async def get_e2e(response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        return {"ok": True, **(await core._run_scoped(profile, e2e_status))}
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise core._unexpected("status", exc, feature="Encrypted notifications")


@router.post("/e2e", dependencies=core._UNLESS_LOCKED)
async def post_e2e(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    # The body is a secret: never cached on the way back either.
    response.headers["Cache-Control"] = "no-store"
    try:
        await core._scoped_acquire(_e2e_limiter, profile)
        body = await core._read_json_body(request, E2E_MAX_BODY_BYTES)
        return {"ok": True, **(await core._run_scoped(profile, lambda: provision_e2e(body)))}
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise core._unexpected("key", exc, feature="Encrypted notifications")


# --- Pairing from Conduit ----------------------------------------------------
#
# The same claim `hermes conduit-push pair CODE` makes, with the code handed
# over the signed-in dashboard connection instead of typed on the host. The
# phone creates the one-time code at its relay and posts it here with that
# relay's URL; this route claims it and saves the credential in the requested
# profile's conduit-push.json, which the hooks re-read on every event, so no
# restart is needed. A re-pair keeps the profile's privacy choice, and its
# encryption key when the same phone and gateway pair again; a new pairing
# drops the key (the phone provisions one for it).

PAIR_LIMIT = 10
PAIR_WINDOW_S = 60.0
PAIR_MAX_BODY_BYTES = 2 * 1024
PAIR_TIMEOUT_S = 15.0
DEFAULT_RELAY_URL = "https://push.milim.dev"
_PAIRING_CODE = re.compile(r"^[A-HJ-NP-Z2-9]{5}-[A-HJ-NP-Z2-9]{5}$")
_pair_limiter = core._MintLimiter(PAIR_LIMIT, PAIR_WINDOW_S, message="Too many pairing requests; try again shortly")
# The claim waits on the relay; it gets its own threads, like the other network routes.
_pair_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="conduit-pair")


def _pair_relay_url(raw: Any, state: Optional[Dict[str, Any]]) -> str:
    """The relay to claim at: the phone's, else this profile's current one, else the default.

    Only a bare http(s) origin with an optional path: no credentials, query or
    fragment, so the claim can't carry anything but the code to that host.
    """
    if raw is None or raw == "":
        stored = (state or {}).get("relay_url")
        if stored:
            # A stored relay gets the same check; one that fails it isn't used.
            with contextlib.suppress(core.TokenError):
                return _pair_relay_url(stored, None)
        return DEFAULT_RELAY_URL
    if not isinstance(raw, str) or len(raw) > 512 or any(not 0x21 <= ord(c) <= 0x7E for c in raw.strip()):
        raise core.TokenError(400, "relay_url must be an http(s) URL")
    try:
        parsed = urllib.parse.urlsplit(raw.strip())
        hostname = parsed.hostname
    except ValueError:  # an unclosed IPv6 bracket, a bad port
        raise core.TokenError(400, "relay_url must be an http(s) URL")
    if parsed.scheme not in ("https", "http") or not hostname or parsed.username or parsed.password \
            or parsed.query or parsed.fragment:
        raise core.TokenError(400, "relay_url must be an http(s) URL")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")).rstrip("/")


_relay_urls_module: Any = None
_relay_urls_lock = threading.Lock()


def _relay_urls() -> Any:
    """relay_urls.py in the plugin's folder, loaded once: the URL rules both pairing paths share."""
    global _relay_urls_module
    with _relay_urls_lock:
        if _relay_urls_module is None:
            path = os.path.join(core._plugin_root(), "relay_urls.py")
            spec = importlib.util.spec_from_file_location("conduit_push_dashboard_relay_urls", path)
            if spec is None or spec.loader is None:
                raise core.TokenError(501, "This plugin install is missing relay_urls.py; update the plugin")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _relay_urls_module = module
        return _relay_urls_module


def _pair_gateway_name(raw: Any, profile: Optional[str]) -> str:
    cleaned = "".join(c for c in raw if c.isprintable()).strip() if isinstance(raw, str) else ""
    if cleaned:
        return cleaned[:80]
    import socket

    name = f"{socket.gethostname()} Hermes"
    scoped = (profile or "").strip()
    return (name if core._limiter_key(scoped) in ("", "default") else f"{name} ({scoped})")[:80]


def _looks_like_json_object(raw: bytes) -> bool:
    # As client.looks_like_json_object: the relay's answer, not a firewall's.
    return raw.lstrip().removeprefix(b"\xef\xbb\xbf").lstrip().startswith(b"{")


def _claim_pairing(relay_url: str, code: str, gateway_name: str) -> Dict[str, Any]:
    """POST /v1/pairings/claim without credentials or redirects: the relay's answer, else TokenError."""
    data = json.dumps({"pairing_code": code, "gateway_name": gateway_name}, separators=(",", ":")).encode("utf-8")
    unreadable = False
    request = urllib.request.Request(f"{relay_url}/v1/pairings/claim", data=data, method="POST", headers={
        "Accept": "application/json", "Content-Type": "application/json",
        "User-Agent": f"Hermes-Conduit-Notifier/{capabilities._plugin_version() or 'unknown'}"})
    try:
        # A plain-HTTP claim (LAN or Tailscale) skips any proxy, which would see the code in clear.
        opener = watch_tools._relay_opener if _relay_urls().is_https(relay_url) else watch_tools._direct_relay_opener
        with opener.open(request, timeout=PAIR_TIMEOUT_S) as response:
            raw, status = response.read(64 * 1024), response.status
    except urllib.error.HTTPError as error:
        status = error.code
        try:
            raw = error.read(4 * 1024)
        except (OSError, http.client.HTTPException):
            raw, unreadable = b"", True
    except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
        raise core.TokenError(502, f"Couldn't reach the push relay from this host ({type(error).__name__})")
    try:
        body = json.loads(raw) if raw else {}
    except ValueError:
        body = {}
    if status == 404:
        raise core.TokenError(410, "This pairing code expired or was already used. Create a new one.")
    if status == 429:
        if isinstance(body, dict) and body.get("error") == "gateway_limit_reached":
            # Every pairing slot this phone has at the relay is taken: waiting won't free one.
            raise core.TokenError(409, "The push relay holds too many pairings for this iPhone. Turn notifications off "
                                  "and on again in Conduit's Settings > Notifications to start fresh, then pair again.")
        raise core.TokenError(429, "The push relay is rate limiting pairing; try again shortly")
    if status == 403 and not unreadable and not _looks_like_json_object(raw):
        # The relay always answers with a JSON error: a bare 403 is something
        # in front of it (a firewall, a rate limiter) blocking this host.
        logger.warning("Conduit plugin: something in front of the push relay answered the pairing claim "
                       "with an empty 403: this host's address is blocked there")
        raise core.TokenError(502, "Something in front of the push relay is blocking this host, so pairing can't get "
                              "through. Pairing again won't help until the relay's operator lifts the block.")
    if not 200 <= status < 300 or not isinstance(body, dict):
        # The relay's own status stays in the log: the answer doesn't probe hosts.
        logger.warning("Conduit plugin: the push relay answered the pairing claim with HTTP %s", status)
        raise core.TokenError(502, "The push relay refused the pairing")
    credential, installation_id = body.get("credential"), body.get("installation_id")
    if not isinstance(credential, str) or not credential or not isinstance(installation_id, str) or not installation_id:
        raise core.TokenError(502, "The push relay sent an unexpected pairing answer")
    return body


def _revoke_replaced_pairing(relay_url: str, credential: str, replaced_by: str = "") -> None:
    """Revokes a credential that a new pairing replaced, off the pairing path.

    The relay mints a new gateway per claim, so without this every re-pair
    leaves a live gateway behind until the phone's slots run out (#250).
    `replaced_by` names the phone it moved to (client.replaced_pairing_query),
    so the Apple Watch's ring follows it. Best effort: the new pairing
    already works either way.
    """
    try:
        url = _relay_urls().check_request_url(f"{relay_url.rstrip('/')}/v1/gateways/current") + replaced_by
    except ValueError:
        # Plain http:// off this machine (a pairing saved before HTTPS was
        # required): the credential isn't sent there.
        return

    def revoke() -> None:
        try:
            status, _body = watch_tools._relay_request(url, "DELETE", credential, None, PAIR_TIMEOUT_S)
            if not 200 <= status < 300 and status != 401:
                logger.info("Conduit plugin: the relay answered revoking the replaced pairing with HTTP %s", status)
        except Exception as exc:
            logger.info("Conduit plugin: the replaced pairing wasn't revoked at the relay: %s", type(exc).__name__)

    try:
        _start_revoke_thread(revoke)
    except RuntimeError as exc:
        # Out of threads. The pairing is saved, and this doesn't undo it.
        logger.warning("Conduit plugin: the replaced pairing wasn't revoked at the relay: %s", exc)


def _start_revoke_thread(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name="conduit-pair-revoke", daemon=True).start()


def pair_profile(body: Any, profile: Optional[str] = None, path: Any = None,
                 claim: Optional[Callable[[str, str, str], Dict[str, Any]]] = None,
                 limit: Optional[Callable[[], None]] = None) -> Dict[str, Any]:
    """Claims the phone's pairing code for this profile. Never returns the credential.

    `limit` runs once the request is well formed, so a mistyped code costs no
    rate-limit slot. The credential this pairing replaces is revoked at its
    relay afterwards.
    """
    if not isinstance(body, dict):
        raise core.TokenError(400, "Expected a JSON object")
    code = body.get("pairing_code")
    code = code.strip().upper() if isinstance(code, str) else ""
    if not _PAIRING_CODE.match(code):
        raise core.TokenError(400, "pairing_code must look like ABCDE-FGHJK")
    path = path if path is not None else core._pairing_state_path()
    relay_url = _pair_relay_url(body.get("relay_url"), core._load_pairing_state(path))
    relay_urls = _relay_urls()
    try:
        # Plain http:// only to a LAN or Tailscale address, before the code is spent.
        relay_urls.check_claim_base(relay_url)
    except relay_urls.RelayURLError as exc:
        raise core.TokenError(400, str(exc))
    gateway_name = _pair_gateway_name(body.get("gateway_name"), profile)
    if limit is not None:
        limit()
    claimed = (claim or _claim_pairing)(relay_url, code, gateway_name)
    state: Dict[str, Any] = {
        "credential": claimed["credential"],
        "gateway_id": claimed.get("gateway_id"),
        "gateway_name": gateway_name,
        "installation_id": claimed["installation_id"],
        "relay_url": relay_url,
    }
    # The relay may name its canonical URL: kept when it is HTTPS on the same
    # origin, or on any origin after a claim over a LAN or Tailscale address.
    try:
        state["relay_url"] = relay_urls.paired_relay_url(claimed.get("relay_url"), relay_url)
    except relay_urls.RelayURLError as exc:
        raise core.TokenError(502, str(exc))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _e2e_lock, _pairing_state_lock(path):
        # Same carry-over as client.claim_pairing: the privacy choice stays.
        previous = core._load_pairing_state(path) or {}
        if previous.get("redact_content"):
            state["redact_content"] = True
        if previous.get("redact_key"):
            state["redact_key"] = previous["redact_key"]
        # Re-pairing the same phone and gateway keeps its encryption key.
        if previous.get("e2e") is not None and previous.get("installation_id") == state["installation_id"] \
                and previous.get("gateway_id") == state["gateway_id"]:
            state["e2e"] = previous["e2e"]
            for field in ("e2e_previous", "rotate_requested_at", "rotate_without_overlap"):
                if field in previous:
                    state[field] = previous[field]
        _save_pairing_state(path, state)
    old_credential, old_relay = previous.get("credential"), previous.get("relay_url")
    if isinstance(old_credential, str) and old_credential and old_credential != state["credential"] \
            and isinstance(old_relay, str) and old_relay:
        _revoke_replaced_pairing(old_relay, old_credential, _relay_urls().replaced_pairing_query(previous, state))
    return {"paired": True, "installation_id": state["installation_id"], "gateway_id": state["gateway_id"],
            "gateway_name": gateway_name, "relay_url": state["relay_url"]}


@router.post("/pair", dependencies=core._UNLESS_LOCKED)
async def post_pair(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        body = await core._read_json_body(request, PAIR_MAX_BODY_BYTES)
        # The dashboard's own profile has one window, named or not.
        key = "" if core._limiter_key(profile) in ("", "default") else core._limiter_key(profile)
        limit = lambda: _pair_limiter.acquire(key)  # noqa: E731
        return {"ok": True, **(await core._run_scoped(profile, lambda: pair_profile(body, profile, limit=limit),
                                                 _pair_executor))}
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException:
        raise
    except Exception as exc:
        raise core._unexpected("pairing", exc, feature="Pairing")
