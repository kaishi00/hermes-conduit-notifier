"""The bridge that carries room turns over Links, for Linked Rooms (designs/cross-owner-agents, N4).

A room's home reaches a member's agent on another Hermes through that
Hermes' runs API (``POST /v1/runs``, its status and stop, and the room
grant routes), with the HermesRoom grant the member's Hermes gave it. Over
a Link those requests go as sealed envelopes through the rooms mailbox
(room_links.py): the home's side (room_bridge_home.py) takes them on a
loopback server and leaves a ``roomlink.request`` for each, and the
member's side (room_bridge_member.py) replays it to its own API server,
answers with a ``roomlink.response``, and sends each run's status as
``roomlink.status`` while it runs. This module has the envelopes' bodies,
the routes both sides carry, and the store both sides keep,
``conduit-rooms/bridge.json`` beside links.json, owner only, under a file
lock as links.json is. Grants travel only inside sealed envelopes and that
file, and are never logged.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

from . import room_identity, room_links

logger = logging.getLogger(__name__)

VERSION = 1
REQUEST, RESPONSE, STATUS = "roomlink.request", "roomlink.response", "roomlink.status"
DIRECTORY = room_identity.DIRECTORY
FILE_NAME = "bridge.json"

# What a grant is bound to: the room, its home and the member, and the profile
# whose agent the member is. Hermes' refresh mints a new grant id for the same
# scope, so a binding is the scope, never the grant.
SCOPE_FIELDS = ("home_install_id", "room_id", "member_id", "target_profile")
METHODS = ("GET", "POST")
# Hermes' hosted_room_peer.MAX_TOKEN_BYTES.
MAX_GRANT_BYTES = 16 * 1024
MAX_PATH_CHARS = 512
MAX_KEY_CHARS = 255

# Hermes' profile ids (hermes_constants.PROFILE_ID_RE), and its room
# identifiers (gateway/hosted_rooms_common.IDENTIFIER_RE, 128 characters).
PROFILE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}\Z")
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_PRINTABLE = re.compile(r"^[\x21-\x7e]+\Z")
_REQUEST_FIELDS = frozenset({"v", "profile", "method", "path", "grant", "key", "body"})
_REQUIRED_FIELDS = frozenset({"v", "profile", "method", "path", "grant"})

# Hermes' run states (tui_gateway/hosted_room_peer_http.py).
ACTIVE = frozenset({"queued", "running", "waiting_for_approval", "stopping"})
ENDED = frozenset({"completed", "failed", "interrupted", "cancelled"})
# A capabilities answer and a refreshed grant, as Hermes' client reads them.
CAPABILITY_FIELDS = ("object", "room_id", "home_install_id", "authority_gateway_id", "authority_epoch", "member_id",
                     "target_profile", "catalog")
GRANT_FIELDS = ("object", "grant", "expires_at", "status_expires_at")
# A run's id, and an error code as Hermes' client reads one.
RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")

# The requests the bridge carries, by their Hermes path; approvals are
# answered by the agent's owner on their own Hermes, never carried.
_RUN = r"(?P<run>[A-Za-z0-9_-]{1,64})"
_ROUTES = (
    ("POST", re.compile(r"/v1/runs"), "dispatch"),
    ("GET", re.compile(rf"/v1/runs/{_RUN}"), "read"),
    ("POST", re.compile(rf"/v1/runs/{_RUN}/stop"), "stop"),
    ("POST", re.compile(rf"/v1/runs/{_RUN}/(?:approval|clarify)"), "approval"),
    ("GET", re.compile(r"/v1/room-members/capabilities"), "capabilities"),
    ("POST", re.compile(r"/v1/room-members/grants/refresh"), "refresh"),
    ("POST", re.compile(r"/v1/room-members/grants/revoke"), "revoke"),
)

_thread_lock = threading.Lock()


class BridgeError(Exception):
    def __init__(self, code: str, message: str, status: int = 503) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class Request:
    """One of Hermes' requests for a member's agent, as a roomlink.request
    carries it: ``key`` is its Idempotency-Key."""

    profile: str
    method: str
    path: str
    grant: str = field(repr=False)
    key: Optional[str] = None
    body: Optional[dict] = None


def request_from(body: Any) -> Optional[Request]:
    """The request a roomlink.request's body holds, or None when it isn't one."""
    if not isinstance(body, dict) or not _REQUIRED_FIELDS <= set(body) <= _REQUEST_FIELDS:
        return None
    if type(body["v"]) is not int or body["v"] != VERSION:
        return None
    profile, method, path, grant = body["profile"], body["method"], body["path"], body["grant"]
    key, payload = body.get("key"), body.get("body")
    if not isinstance(profile, str) or not (profile == "default" or PROFILE.match(profile)):
        return None
    if method not in METHODS or not isinstance(path, str) or not path.startswith("/") \
            or len(path) > MAX_PATH_CHARS or not _PRINTABLE.match(path):
        return None
    if not isinstance(grant, str) or len(grant) > MAX_GRANT_BYTES or not _PRINTABLE.match(grant):
        return None
    if key is not None and (not isinstance(key, str) or len(key) > MAX_KEY_CHARS or not _PRINTABLE.match(key)):
        return None
    if payload is not None and not isinstance(payload, dict):
        return None
    return Request(profile=profile, method=method, path=path, grant=grant, key=key, body=payload)


def route(method: str, path: str) -> tuple[Optional[str], Optional[str]]:
    """The route a request is (``dispatch``, ``read``, ``stop``,
    ``approval``, ``capabilities``, ``refresh``, ``revoke``) and its run's
    id, or None and None for one the bridge doesn't carry."""
    for route_method, pattern, name in _ROUTES:
        found = pattern.fullmatch(path) if method == route_method else None
        if found is not None:
            return name, found.groupdict().get("run")
    return None, None


def response_body(re_id: str, status: int, body: dict, run: Optional[dict] = None) -> dict:
    """A roomlink.response's body: the member's answer to the request whose
    message id is ``re_id``, and on an admission, the run's status then."""
    answer = {"v": VERSION, "re": re_id, "status": status, "body": body}
    if run is not None:
        answer["run"] = run
    return answer


def status_body(run_id: str, seq: int, shown: Mapping[str, Any]) -> dict:
    """A roomlink.status's body: a run's status as the member shows it,
    ``seq`` growing with each one sent for the run."""
    body = {"v": VERSION, "run_id": run_id, "seq": seq, "status": shown["status"]}
    for name in ("output", "error"):
        if name in shown:
            body[name] = shown[name]
    return body


def cut(text: str, limit: int) -> str:
    """``text`` cut to ``limit`` bytes of UTF-8, at a character. A lone
    surrogate, which JSON carries and UTF-8 can't, reads as ``?``."""
    return text.encode("utf-8", "replace")[:limit].decode("utf-8", "ignore")


def grant_scope(grant: Any) -> Optional[dict[str, str]]:
    """The scope a HermesRoom grant says it's for, read from its payload
    (its first part, base64url JSON), or None when it can't be read. Only
    the member's API server can check the grant's signature; this is what
    it would be bound by."""
    if not isinstance(grant, str) or len(grant) > MAX_GRANT_BYTES:
        return None
    encoded, dot, _ = grant.partition(".")
    if not dot or not encoded:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("ascii"))
    except (binascii.Error, ValueError, UnicodeDecodeError, RecursionError):
        return None
    return checked_scope({name: payload.get(name) for name in SCOPE_FIELDS}) if isinstance(payload, dict) \
        else None


def checked_scope(value: Any) -> Optional[dict[str, str]]:
    """``value`` as a scope: exactly SCOPE_FIELDS, each an identifier (the
    profile a profile id), or None."""
    if not isinstance(value, Mapping) or set(value) != set(SCOPE_FIELDS):
        return None
    if not all(isinstance(value[name], str) and IDENTIFIER.match(value[name]) for name in SCOPE_FIELDS):
        return None
    profile = value["target_profile"]
    if not (profile == "default" or PROFILE.match(profile)):
        return None
    return {name: value[name] for name in SCOPE_FIELDS}


def start(root: Any) -> None:
    """Starts the bridge for the installation at ``root`` in this process,
    on the room_links copy this module came with: the member's side, which
    answers roomlink.request and watches the runs it admitted, and the
    home's, which takes this Hermes' requests on its loopback server. Each
    runs while that copy's loop holds the lease. Calling it again does
    nothing."""
    from . import room_bridge_home, room_bridge_member

    room_bridge_member.start(root)
    room_bridge_home.start(root)


def url(root: Any, link_id: Any, profile: Any) -> str:
    """The base URL Hermes reaches a member's agent at over the Link
    ``link_id``: the agent's profile on the Hermes at its other end, at the
    port the home's side keeps (room_bridge_home.py). A room's route for the
    agent points there. ValueError for what isn't a Link's id or a profile,
    BridgeError (not_listening, 503) before the bridge has listened."""
    if not room_links._is_id(link_id):
        raise ValueError("That isn't a Link's id")
    if not isinstance(profile, str) or not PROFILE.match(profile):
        raise ValueError("That isn't a profile's name")
    port = Store(root).read()["home"]["port"]
    if port is None:
        raise BridgeError("not_listening", "Linked Rooms' bridge hasn't listened on this Hermes yet; it does once "
                                           "there's a Link", 503)
    return f"http://127.0.0.1:{port}/l/{link_id}/p/{profile}"


# --- The store ----------------------------------------------------------------------


class Store:
    """``<root>/conduit-rooms/bridge.json``. Each side keeps its own section
    (``member`` and ``home``); one this plugin doesn't know is kept."""

    def __init__(self, root: Any) -> None:
        self.directory = Path(root) / DIRECTORY
        self.path = self.directory / FILE_NAME

    def read(self) -> dict[str, Any]:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return _empty()
        except OSError as exc:
            raise BridgeError("unreadable", f"Couldn't read {self.path} ({exc.strerror or exc})") from None
        try:
            state = json.loads(raw)
        except (ValueError, RecursionError):
            state = None
        if not _well_formed(state):
            raise BridgeError("unreadable", f"{self.path} isn't a bridge file this plugin wrote; moving it aside "
                                            "ends the rooms it carries until they're joined again")
        for name, section in _empty().items():
            state.setdefault(name, section)
        return state

    @contextmanager
    def update(self) -> Iterator[dict[str, Any]]:
        """The state to change, saved when the block ends without raising."""
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        lock_path = self.directory / f".{FILE_NAME}.lock"
        with _thread_lock, open(lock_path, "a+b") as handle, room_identity._exclusive(handle):
            state = self.read()
            before = json.dumps(state, sort_keys=True)
            yield state
            if json.dumps(state, sort_keys=True) != before:
                self._save(state)

    def _save(self, state: dict[str, Any]) -> None:
        encoded = json.dumps(state, separators=(",", ":"), sort_keys=True, ensure_ascii=False) + "\n"
        descriptor, name = tempfile.mkstemp(prefix=f".{FILE_NAME}.", suffix=".tmp", dir=self.directory)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        self.path.chmod(0o600)
        room_identity._sync_directory(self.directory)


def _empty() -> dict[str, Any]:
    return {"version": VERSION, "member": {"bindings": {}, "runs": {}, "days": {}},
            "home": {"port": None, "runs": {}, "kept": {}, "outbox": []}}


def _well_formed(state: Any) -> bool:
    if not isinstance(state, dict) or type(state.get("version")) is not int or state["version"] != VERSION:
        return False
    empty = _empty()
    member, home = state.get("member", empty["member"]), state.get("home", empty["home"])
    if not isinstance(member, dict) or not all(isinstance(member.get(name), dict)
                                               for name in ("bindings", "runs", "days")):
        return False
    if not isinstance(home, dict) or not all(isinstance(home.get(name), dict) for name in ("runs", "kept")) \
            or not isinstance(home.get("outbox"), list):
        return False
    port = home.get("port", "")
    return all(isinstance(scopes, list) and all(checked_scope(scope) is not None for scope in scopes)
               for scopes in member["bindings"].values()) \
        and all(_kept_run(run) for run in member["runs"].values()) \
        and all(isinstance(rooms, dict) and all(isinstance(today, dict) and isinstance(today.get("keys"), list)
                                                and isinstance(today.get("notice"), bool)
                                                for today in rooms.values())
                for rooms in member["days"].values()) \
        and (port is None or (type(port) is int and 0 < port < 65536)) \
        and all(_carried_run(run) for run in home["runs"].values()) \
        and all(_kept_admission(kept) for kept in home["kept"].values()) \
        and all(isinstance(item, dict) and isinstance(item.get("id"), str) and isinstance(item.get("link"), str)
                and _number(item.get("at")) and isinstance(item.get("request"), dict) for item in home["outbox"])


def _kept_run(run: Any) -> bool:
    """A run as the member's side keeps it, with every field its watching
    reads: one entry without them would stop the watching of every run."""
    if not isinstance(run, dict) or not isinstance(run.get("link"), str):
        return False
    if run.get("kind") == "pass":
        return isinstance(run.get("output"), str) and _number(run.get("at"))
    return run.get("kind") == "run" and isinstance(run.get("profile"), str) and isinstance(run.get("grant"), str) \
        and _number(run.get("admitted")) and _number(run.get("sent_at")) and type(run.get("seq")) is int \
        and _shown(run.get("shown")) and (run.get("ended") is None or _number(run.get("ended")))


def _carried_run(run: Any) -> bool:
    """A run as the home's side keeps it, with every field its answers and
    its watching read."""
    return isinstance(run, dict) and all(isinstance(run.get(name), str)
                                         for name in ("link", "run", "profile", "grant", "name")) \
        and _digests(run.get("grants")) and _number(run.get("admitted")) and _number(run.get("heard")) \
        and type(run.get("seq")) is int \
        and _shown(run.get("shown")) and "ended" in run and (run["ended"] is None or _number(run["ended"]))


def _kept_admission(kept: Any) -> bool:
    """An admission as the home's side keeps it, for Hermes' retry."""
    return isinstance(kept, dict) and all(isinstance(kept.get(name), str) for name in ("run", "status", "profile")) \
        and _number(kept.get("at")) and _digests(kept.get("grants")) and type(kept.get("own")) is bool


def _digests(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _shown(value: Any) -> bool:
    return isinstance(value, dict) and isinstance(value.get("status"), str)


def _number(value: Any) -> bool:
    return type(value) in (int, float)
