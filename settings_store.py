"""Hermes configures Conduit: the phone's settings as Conduit last showed them,
and the changes Hermes asked for.

Conduit stays the owner of its own settings. Each Conduit (one per iPhone or
iPad, by its installation id) uploads the settings it lets Hermes see: each
one's key, plain-language title and description, kind and choices, its tier
and its value, plus the user's "Let Hermes change settings" mode. Conduit
declares the list, so a newer Conduit can offer more without a plugin update,
and anything it doesn't list is out of Hermes' reach.

The ``conduit_settings`` tool reads that snapshot and asks for changes. A
change is only a request kept here: Conduit picks it up the next time it
syncs, checks it against its own list and tier again, applies a small one
(with Undo) or asks the user, and reports what happened. The phone is the
gate, not this file: the agent can edit any file on the host.

A request carries the value Hermes saw (``from``). When the user changed the
setting on the phone since, Conduit keeps the user's value and reports a
conflict.

State lives in the profile's ``conduit-settings.json`` beside the pairing
state. The tool (agent process) and the dashboard route (dashboard process)
both use this module: the dashboard loads it by path, so it imports nothing
from the plugin package.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
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

FILE_NAME = "conduit-settings.json"
VERSION = 1

MODES = ("off", "ask", "small")
TIERS = ("small", "ask")
KINDS = ("bool", "choice", "text")
# What a phone reports back for a request it took.
RESULTS = ("applied", "dismissed", "conflict", "refused", "undone")

MAX_DEVICES = 8
MAX_SETTINGS = 80
MAX_CHOICES = 40
MAX_KEY_CHARS = 64
MAX_TITLE_CHARS = 80
MAX_DESCRIPTION_CHARS = 400
MAX_TEXT_CHARS = 300
MAX_REASON_CHARS = 200
MAX_NOTE_CHARS = 200
MAX_DEVICE_NAME_CHARS = 60
MAX_CHANGES = 10
MAX_PENDING = 20
MAX_RESULTS = 20
# Requests no phone picked up are dropped after a day; results are kept a
# week so the next chat can still tell what happened.
PENDING_TTL_S = 24 * 3600
RESULT_TTL_S = 7 * 24 * 3600
# Changes Hermes may ask for per device in an hour. Advisory: a device is
# whatever installation id the dashboard caller names, so the route's own
# limiter and the dashboard's auth are the boundary.
PER_HOUR = 20
HOUR_S = 3600
# A device not seen for this long is forgotten.
DEVICE_TTL_S = 60 * 24 * 3600

_KEY = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")
_INSTALLATION = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

_thread_lock = threading.Lock()


class SettingsError(ValueError):
    """A request this store won't keep; ``code`` is a short machine word."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SettingsStore:
    def __init__(self, home: Path, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(home) / FILE_NAME
        self.clock = clock

    # --- The phone's side ----------------------------------------------------

    def sync(self, body: Any) -> dict[str, Any]:
        """A phone's upload: its settings and mode, and what became of the
        requests it took. Returns the requests still waiting for it."""
        if not isinstance(body, dict):
            raise SettingsError("invalid", "Expected a JSON object")
        installation = body.get("installation_id")
        if not isinstance(installation, str) or not _INSTALLATION.match(installation):
            raise SettingsError("invalid", "installation_id must be a plain id")
        mode = body.get("mode")
        if mode not in MODES:
            raise SettingsError("invalid", f"mode must be one of {', '.join(MODES)}")
        settings = _settings(body.get("settings"))
        results = body.get("results", [])
        if not isinstance(results, list) or len(results) > MAX_PENDING * 2:
            raise SettingsError("invalid", "results must be a short list")
        device = {
            "name": _line(body.get("device"), MAX_DEVICE_NAME_CHARS) or "Conduit",
            "app_version": _line(body.get("app_version"), 40),
            "mode": mode,
            "settings": settings,
        }
        with self._locked() as state:
            now = self.clock()
            device["seen_at"] = now
            state["devices"][installation] = device
            _trim_devices(state)
            for result in results:
                _take_result(state, installation, result, now)
            pending = [_public_request(request) for request in state["pending"]
                       if request["installation_id"] == installation]
        return {"pending": pending}

    # --- Hermes' side --------------------------------------------------------

    def view(self) -> dict[str, Any]:
        """Every device Conduit reported, most recently seen first, with the
        requests waiting and the recent results."""
        state = self._load()
        devices = sorted(state["devices"].items(), key=lambda item: -item[1]["seen_at"])
        return {
            "devices": [{"id": installation, **device} for installation, device in devices],
            "pending": [_public_request(request) for request in state["pending"]],
            "results": list(state["results"]),
        }

    def has_device(self) -> bool:
        return bool(self._load()["devices"])

    def request(self, changes: Any, reason: Any, *, device: Any = None, session_id: Any = None) -> dict[str, Any]:
        """Keeps Hermes' requested changes for one device (the most recently
        seen when ``device`` is None). Raises SettingsError when it won't."""
        clean_reason = _line(reason, MAX_REASON_CHARS)
        if not clean_reason:
            raise SettingsError("no_reason", "Give a short reason the user will read.")
        if not isinstance(changes, list) or not changes:
            raise SettingsError("no_changes", "Give at least one change: a key and its new value.")
        if len(changes) > MAX_CHANGES:
            raise SettingsError("too_many", f"Ask for at most {MAX_CHANGES} changes at once.")
        session = session_id.strip()[:180] if isinstance(session_id, str) else ""
        with self._locked() as state:
            now = self.clock()
            installation = _pick_device(state, device)
            target = state["devices"][installation]
            if target["mode"] == "off":
                raise SettingsError("off", "The user hasn't let Hermes change Conduit settings on this device.")
            by_key = {setting["key"]: setting for setting in target["settings"]}
            prepared: list[dict[str, Any]] = []
            for change in changes:
                if not isinstance(change, dict):
                    raise SettingsError("invalid", "Each change is an object with key and value.")
                key = change.get("key")
                setting = by_key.get(key) if isinstance(key, str) else None
                if setting is None:
                    raise SettingsError("unknown_key", f"Conduit doesn't let Hermes change {key!r}.")
                value = _valid_value(setting, change.get("value"))
                if value is None:
                    raise SettingsError("invalid_value", _value_hint(setting))
                if value == setting["value"]:
                    continue
                prepared.append({"key": setting["key"], "value": value, "from": setting["value"],
                                 "tier": setting["tier"], "title": setting["title"]})
            if not prepared:
                return {"device": installation, "requests": [], "unchanged": True}
            recent = [at for at in state["asked"].get(installation, []) if now - at < HOUR_S]
            if len(recent) + len(prepared) > PER_HOUR:
                raise SettingsError("rate_limited", "Too many setting changes this hour. Try again later.")
            # A newer request for the same setting replaces the older one.
            keys = {item["key"] for item in prepared}
            state["pending"] = [request for request in state["pending"]
                                if not (request["installation_id"] == installation and request["key"] in keys)]
            made = []
            for item in prepared:
                request = {"id": secrets.token_urlsafe(12), "installation_id": installation, **item,
                           "reason": clean_reason, "session_id": session, "at": now}
                state["pending"].append(request)
                made.append(_public_request(request))
            mine = [request for request in state["pending"] if request["installation_id"] == installation]
            if len(mine) > MAX_PENDING:
                # The cap is per device, so a burst for one never drops another's.
                dropped = {request["id"] for request in mine[:len(mine) - MAX_PENDING]}
                state["pending"] = [request for request in state["pending"] if request["id"] not in dropped]
            state["asked"][installation] = (recent + [now] * len(prepared))[-PER_HOUR:]
            return {"device": installation, "mode": target["mode"], "requests": made}

    def cancel(self, request_ids: Any) -> int:
        """Drops waiting requests Hermes takes back; how many were dropped."""
        ids = {value for value in request_ids if isinstance(value, str)} if isinstance(request_ids, list) else set()
        with self._locked() as state:
            before = len(state["pending"])
            state["pending"] = [request for request in state["pending"] if request["id"] not in ids]
            return before - len(state["pending"])

    # --- Storage -------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            raw = None
        return _normalized(raw, self.clock())

    @contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        """Load, let the caller change, then save: all under the file lock."""
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self.path.with_name(f".{self.path.name}.lock")
        with _thread_lock, open(lock_path, "a+b") as handle, _exclusive(handle):
            state = self._load()
            before = _encoded(state)
            yield state
            after = _encoded(state)
            if after != before or not self.path.exists():
                self._save(after)

    def _save(self, encoded: str) -> None:
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(encoded)
        temporary.chmod(0o600)
        temporary.replace(self.path)
        self.path.chmod(0o600)


# --- Helpers ---------------------------------------------------------------------


def _encoded(state: dict[str, Any]) -> str:
    return json.dumps(state, separators=(",", ":"), ensure_ascii=False) + "\n"


def _line(value: Any, limit: int) -> str:
    """One line of printable text, trimmed; empty when ``value`` isn't text."""
    if not isinstance(value, str):
        return ""
    # Line breaks, tabs and other controls become spaces, not glue.
    text = " ".join("".join(char if char.isprintable() else " " for char in value).split())
    return text[:limit]


def _settings(value: Any) -> list[dict[str, Any]]:
    """The settings a phone listed, each well-formed, or SettingsError."""
    if not isinstance(value, list) or len(value) > MAX_SETTINGS:
        raise SettingsError("invalid", f"settings must be a list of up to {MAX_SETTINGS}")
    kept: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value:
        setting = _setting(raw)
        if setting is None:
            raise SettingsError("invalid", "Each setting needs a key, title, kind, tier and a value of its kind")
        if setting["key"] in seen:
            raise SettingsError("invalid", f"setting {setting['key']} is listed twice")
        seen.add(setting["key"])
        kept.append(setting)
    return kept


def _setting(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    key = raw.get("key")
    title = _line(raw.get("title"), MAX_TITLE_CHARS)
    kind = raw.get("kind")
    tier = raw.get("tier")
    if not isinstance(key, str) or not _KEY.match(key) or not title or kind not in KINDS or tier not in TIERS:
        return None
    setting: dict[str, Any] = {
        "key": key, "title": title, "kind": kind, "tier": tier,
        "description": _line(raw.get("description"), MAX_DESCRIPTION_CHARS),
        "section": _line(raw.get("section"), MAX_TITLE_CHARS),
    }
    if kind == "choice":
        choices = raw.get("choices")
        if not isinstance(choices, list) or not 1 <= len(choices) <= MAX_CHOICES:
            return None
        kept = []
        for choice in choices:
            if not isinstance(choice, dict) or not isinstance(choice.get("value"), str):
                return None
            option = _line(choice["value"], MAX_KEY_CHARS)
            if not option or option != choice["value"]:
                return None
            kept.append({"value": option, "title": _line(choice.get("title"), MAX_TITLE_CHARS) or option})
        setting["choices"] = kept
    if kind == "text":
        limit = raw.get("max_length")
        setting["max_length"] = limit if isinstance(limit, int) and not isinstance(limit, bool) \
            and 1 <= limit <= MAX_TEXT_CHARS else MAX_TEXT_CHARS
    value = _valid_value(setting, raw.get("value"))
    if value is None:
        return None
    setting["value"] = value
    return setting


def _valid_value(setting: dict[str, Any], value: Any) -> Any:
    """``value`` as this setting keeps it, or None when it isn't one."""
    kind = setting["kind"]
    if kind == "bool":
        return value if isinstance(value, bool) else None
    if kind == "choice":
        return value if isinstance(value, str) and any(choice["value"] == value for choice in setting["choices"]) else None
    if kind == "text":
        if not isinstance(value, str):
            return None
        text = _line(value, MAX_TEXT_CHARS + 1)
        return text if len(text) <= setting["max_length"] else None
    return None


def _value_hint(setting: dict[str, Any]) -> str:
    if setting["kind"] == "bool":
        return f"{setting['key']} takes true or false."
    if setting["kind"] == "choice":
        return f"{setting['key']} takes one of: " + ", ".join(choice["value"] for choice in setting["choices"]) + "."
    return f"{setting['key']} takes text of up to {setting['max_length']} characters."


def _pick_device(state: dict[str, Any], device: Any) -> str:
    devices = state["devices"]
    if not devices:
        raise SettingsError("no_device", "No Conduit has shared its settings with this Hermes profile yet.")
    if device is None or device == "":
        return max(devices, key=lambda installation: devices[installation]["seen_at"])
    if isinstance(device, str):
        if device in devices:
            return device
        named = [installation for installation, entry in devices.items() if entry["name"].casefold() == device.casefold()]
        if len(named) == 1:
            return named[0]
    raise SettingsError("unknown_device", "No such device. Leave device out for the one the user used last.")


def _public_request(request: dict[str, Any]) -> dict[str, Any]:
    return {key: request[key] for key in ("id", "key", "title", "value", "from", "tier", "reason", "session_id", "at")
            if key in request} | {"device": request["installation_id"]}


def _take_result(state: dict[str, Any], installation: str, result: Any, now: float) -> None:
    """A phone's answer to one of its requests: the request leaves the queue
    and its result is kept for Hermes."""
    if not isinstance(result, dict):
        return
    request_id = result.get("id")
    status = result.get("status")
    if not isinstance(request_id, str) or not _REQUEST_ID.match(request_id) or status not in RESULTS:
        return
    request = next((item for item in state["pending"]
                    if item["id"] == request_id and item["installation_id"] == installation), None)
    if request is not None:
        state["pending"].remove(request)
    elif status != "undone":
        return
    else:
        # An Undo comes after the request was applied and left the queue.
        earlier = next((item for item in state["results"] if item["id"] == request_id
                        and item["device"] == installation), None)
        if earlier is None:
            return
        request = {**earlier, "installation_id": installation}
    state["results"] = [item for item in state["results"] if item["id"] != request_id]
    state["results"].append({
        "id": request_id, "device": installation, "key": request["key"], "title": request.get("title", ""),
        "value": request["value"], "from": request.get("from"), "status": status,
        "note": _line(result.get("note"), MAX_NOTE_CHARS), "session_id": request.get("session_id", ""), "at": now,
    })
    state["results"] = state["results"][-MAX_RESULTS:]


def _trim_devices(state: dict[str, Any]) -> None:
    devices = state["devices"]
    if len(devices) <= MAX_DEVICES:
        return
    for installation in sorted(devices, key=lambda item: devices[item]["seen_at"])[:len(devices) - MAX_DEVICES]:
        del devices[installation]
        state["pending"] = [request for request in state["pending"] if request["installation_id"] != installation]


def _normalized(raw: Any, now: float) -> dict[str, Any]:
    """A well-formed state from whatever is on disk, with expired entries
    dropped."""
    state: dict[str, Any] = {"version": VERSION, "devices": {}, "pending": [], "results": [], "asked": {}}
    if not isinstance(raw, dict):
        return state
    asked = raw.get("asked")
    devices = raw.get("devices")
    for installation, device in devices.items() if isinstance(devices, dict) else ():
        if not isinstance(installation, str) or not _INSTALLATION.match(installation) or not isinstance(device, dict):
            continue
        seen = device.get("seen_at")
        if not isinstance(seen, (int, float)) or now - seen > DEVICE_TTL_S or device.get("mode") not in MODES:
            continue
        try:
            settings = _settings(device.get("settings"))
        except SettingsError:
            continue
        state["devices"][installation] = {"name": _line(device.get("name"), MAX_DEVICE_NAME_CHARS) or "Conduit",
                                          "app_version": _line(device.get("app_version"), 40),
                                          "mode": device["mode"], "settings": settings, "seen_at": float(seen)}
    pending = raw.get("pending")
    for request in pending if isinstance(pending, list) else []:
        if (isinstance(request, dict) and isinstance(request.get("installation_id"), str)
                and request["installation_id"] in state["devices"]
                and isinstance(request.get("id"), str) and isinstance(request.get("key"), str)
                and "value" in request and request.get("tier") in TIERS
                and isinstance(request.get("at"), (int, float)) and now - request["at"] <= PENDING_TTL_S):
            state["pending"].append(request)
    state["pending"] = state["pending"][-MAX_PENDING * MAX_DEVICES:]
    results = raw.get("results")
    for result in results if isinstance(results, list) else []:
        if (isinstance(result, dict) and isinstance(result.get("id"), str) and isinstance(result.get("device"), str)
                and isinstance(result.get("key"), str) and "value" in result and result.get("status") in RESULTS
                and isinstance(result.get("at"), (int, float)) and now - result["at"] <= RESULT_TTL_S):
            state["results"].append(result)
    state["results"] = state["results"][-MAX_RESULTS:]
    if isinstance(asked, dict):
        for installation, times in asked.items():
            if installation in state["devices"] and isinstance(times, list):
                kept = [at for at in times if isinstance(at, (int, float)) and now - at < HOUR_S]
                if kept:
                    state["asked"][installation] = kept[-PER_HOUR:]
    return state


@contextmanager
def _exclusive(handle: Any) -> Iterator[None]:
    # The same locking as calls_store: flock where there is one, a
    # first-byte lock on Windows.
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    elif msvcrt is not None:
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
