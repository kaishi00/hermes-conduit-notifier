"""The queue between a bot's ``conduit_user_computer`` calls and the user's
computers running Conduit (Control from Hermes).

Tools run in the gateway and Conduit for desktop talks to the dashboard, two
processes that share only this profile's home, so the queue is files under
``conduit-pc/`` there:

- ``computers.json``: the computers that offer control, by id: the name the
  user gave it, its platform and when it last asked for work. One that asked
  in the last ``ONLINE_S`` seconds is online.
- ``requests/<id>.json``: one action a chat asked for, waiting for its
  computer. ``.claimed`` once the computer took it.
- ``results/<id>.json``: the computer's answer, read once by the waiting tool.
- ``grants.json``: which chat each computer let in, and when it was last used.
  A computer is driven by one chat at a time: the user allows a chat once
  (Eric 2026-11-27: ask once per chat), and allowing another chat takes it
  over. Stop forgets the grant, so the next action asks again.

Nothing here holds what was typed or seen beyond the one round trip: the
request and result files are removed when the tool reads the answer, and the
log keeps the action, the chat and the time only.

The dashboard loads this file by path (it can't import the plugin), so it
imports nothing of the plugin's.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import tempfile
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
except ImportError:
    msvcrt = None  # type: ignore[assignment]

logger = logging.getLogger("hermes.plugins.conduit_push")

DIR = "conduit-pc"
ONLINE_S = 45.0
# A computer that offered control in the last 30 days keeps the tool listed:
# Hermes caches a tool's availability per process, so it can't follow a
# computer going on and offline. The tool says when it's offline.
KNOWN_S = 30 * 86400.0
# A claimed request whose computer never answered is dropped after this.
STALE_S = 300.0
LOG_MAX_LINES = 500
MAX_NAME_CHARS = 64
MAX_TITLE_CHARS = 120
ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
PLATFORMS = ("windows", "mac", "linux")
GRANT_STATES = ("allowed", "denied")


class PcControlError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def clean_id(value: Any, what: str = "id") -> str:
    if not isinstance(value, str) or not ID_RE.match(value):
        raise PcControlError(400, f"{what} must be 8 to 64 letters, digits, - or _")
    return value


def clean_name(value: Any) -> str:
    text = " ".join(str(value or "").split())[:MAX_NAME_CHARS]
    return text or "Computer"


class PcControlStore:
    def __init__(self, home: Path | str, clock: Callable[[], float] = time.time) -> None:
        self.root = Path(home) / DIR
        self.clock = clock

    # --- computers ------------------------------------------------------------

    def note_computer(self, computer: str, name: Any, platform: Any) -> dict[str, Any]:
        """A computer asked for work: it's online, under the name it gave."""
        computer = clean_id(computer, "computer")
        platform = platform if platform in PLATFORMS else "windows"
        now = self.clock()
        with self._json("computers.json") as state:
            computers = state.setdefault("computers", {})
            entry = computers.get(computer) if isinstance(computers.get(computer), dict) else {}
            # A write each time would rewrite the file every long poll; the
            # online window is far longer than this.
            if entry.get("name") != clean_name(name) or entry.get("platform") != platform \
                    or now - float(entry.get("seen") or 0) > 10.0:
                computers[computer] = {"name": clean_name(name), "platform": platform, "seen": now}
            return dict(computers[computer])

    def computers(self) -> dict[str, dict[str, Any]]:
        state = self._read("computers.json")
        computers = state.get("computers") if isinstance(state.get("computers"), dict) else {}
        return {key: value for key, value in computers.items() if isinstance(value, dict)}

    def online(self) -> dict[str, dict[str, Any]]:
        now = self.clock()
        return {key: value for key, value in self.computers().items()
                if now - float(value.get("seen") or 0) <= ONLINE_S}

    def known(self) -> bool:
        now = self.clock()
        return any(now - float(value.get("seen") or 0) <= KNOWN_S for value in self.computers().values())

    def forget_computer(self, computer: str) -> None:
        """Control turned off on that computer: it no longer offers it."""
        computer = clean_id(computer, "computer")
        with self._json("computers.json") as state:
            (state.get("computers") or {}).pop(computer, None)
        self.stop(computer)

    # --- grants ---------------------------------------------------------------

    def grant(self, computer: str) -> dict[str, Any] | None:
        state = self._read("grants.json")
        entry = (state.get("computers") or {}).get(computer)
        return entry if isinstance(entry, dict) else None

    def needs_ok(self, computer: str, session: str) -> bool:
        entry = self.grant(computer)
        return not (entry and entry.get("session") == session and entry.get("state") == "allowed")

    def record_grant(self, computer: str, session: str, state: str, chat: str = "") -> None:
        if state not in GRANT_STATES:
            return
        with self._json("grants.json") as grants:
            computers = grants.setdefault("computers", {})
            current = computers.get(computer) if isinstance(computers.get(computer), dict) else None
            if state == "denied" and current and current.get("session") != session:
                # Turning a chat down leaves the chat that has it in place.
                return
            computers[computer] = {"session": session, "chat": chat[:MAX_TITLE_CHARS], "state": state,
                                   "at": self.clock(), "used": self.clock()}

    def touch_grant(self, computer: str, session: str) -> None:
        with self._json("grants.json") as grants:
            entry = (grants.get("computers") or {}).get(computer)
            if isinstance(entry, dict) and entry.get("session") == session:
                entry["used"] = self.clock()

    def stop(self, computer: str) -> int:
        """The user stopped control on that computer: the grant goes, and
        every action still waiting is answered as stopped. The number of
        actions stopped."""
        computer = clean_id(computer, "computer")
        with self._json("grants.json") as grants:
            (grants.get("computers") or {}).pop(computer, None)
        stopped = 0
        folder = self._dir("requests")
        for request in self._requests(include_claimed=True):
            if request.get("computer") != computer:
                continue
            # A waiting one is taken off the queue first, so the computer's next
            # poll can't claim and run it after Stop.
            waiting = folder / f"{request['id']}.json"
            try:
                os.replace(waiting, waiting.with_suffix(".claimed"))
            except OSError:
                pass  # already claimed
            # One the computer already answered keeps its answer: it ran.
            result = self._dir("results") / f"{request['id']}.json"
            if not result.exists():
                self._write_file(result, {"ok": False, "code": "stopped"})
            stopped += 1
        return stopped

    # --- requests -------------------------------------------------------------

    def add_request(self, computer: str, session: str, chat: str, action: str, args: dict[str, Any]) -> dict[str, Any]:
        request = {"id": secrets.token_urlsafe(12), "computer": computer, "session": session,
                   "chat": chat[:MAX_TITLE_CHARS], "action": action, "args": args, "at": self.clock(),
                   "needs_ok": self.needs_ok(computer, session)}
        current = self.grant(computer)
        if request["needs_ok"] and current and current.get("state") == "allowed" \
                and current.get("session") != session:
            request["takes_over"] = str(current.get("chat") or "")
        self._write_file(self._dir("requests") / f"{request['id']}.json", request)
        return request

    def claim(self, computer: str) -> list[dict[str, Any]]:
        """The requests waiting for this computer, oldest first, marked taken
        so a second poll doesn't run them again."""
        computer = clean_id(computer, "computer")
        taken = []
        for request in self._requests(include_claimed=False):
            if request.get("computer") != computer:
                continue
            path = self._dir("requests") / f"{request['id']}.json"
            try:
                os.replace(path, path.with_suffix(".claimed"))
            except OSError:
                continue  # another poll took it, or the tool gave up on it
            taken.append(request)
        return sorted(taken, key=lambda item: float(item.get("at") or 0))

    def has_waiting(self, computer: str) -> bool:
        return any(request.get("computer") == computer for request in self._requests(include_claimed=False))

    def answer(self, request_id: str, result: dict[str, Any], computer: str) -> bool:
        """The computer's answer to one of its requests. False for a request
        that isn't (or is no longer) waiting for that computer."""
        request_id = clean_id(request_id, "request id")
        folder = self._dir("requests")
        request = None
        for suffix in (".claimed", ".json"):
            path = folder / f"{request_id}{suffix}"
            data = _load(path)
            if isinstance(data, dict):
                request = data
                break
        if request is None or request.get("computer") != computer:
            return False
        self._write_file(self._dir("results") / f"{request_id}.json", result)
        return True

    def take_result(self, request_id: str) -> dict[str, Any] | None:
        path = self._dir("results") / f"{request_id}.json"
        data = _load(path)
        if data is None:
            return None
        self.drop(request_id)
        return data if isinstance(data, dict) else {"ok": False, "code": "bad_answer"}

    def drop(self, request_id: str) -> None:
        for path in (self._dir("requests") / f"{request_id}.json", self._dir("requests") / f"{request_id}.claimed",
                     self._dir("results") / f"{request_id}.json"):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                logger.debug("conduit_push: couldn't remove %s", path, exc_info=True)

    # --- log ------------------------------------------------------------------

    def log(self, computer: str, session: str, action: str, outcome: str) -> None:
        """When, which chat and which action, never what was typed or seen."""
        path = self.root / "log.jsonl"
        line = json.dumps({"at": self.clock(), "computer": computer, "session": session, "action": action,
                           "outcome": outcome})
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with _file_lock(path):
                lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
                lines = (lines + [line])[-LOG_MAX_LINES:]
                self._write_text(path, "\n".join(lines) + "\n")
        except OSError:
            logger.debug("conduit_push: couldn't write the computer control log", exc_info=True)

    # --- files ----------------------------------------------------------------

    def _requests(self, include_claimed: bool) -> list[dict[str, Any]]:
        folder = self.root / "requests"
        if not folder.is_dir():
            return []
        now = self.clock()
        found = []
        for path in folder.iterdir():
            # Claimed ones are read too, so one whose tool went away (a gateway
            # restart) is swept with its result, screenshot and all.
            if path.suffix not in (".json", ".claimed"):
                continue
            data = _load(path)
            if not isinstance(data, dict) or not isinstance(data.get("id"), str):
                continue
            if now - float(data.get("at") or 0) > STALE_S:
                self.drop(data["id"])
                continue
            if path.suffix == ".json" or include_claimed:
                found.append(data)
        return found

    def _dir(self, name: str) -> Path:
        folder = self.root / name
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _read(self, name: str) -> dict[str, Any]:
        data = _load(self.root / name)
        return data if isinstance(data, dict) else {}

    @contextmanager
    def _json(self, name: str) -> Iterator[dict[str, Any]]:
        path = self.root / name
        self.root.mkdir(parents=True, exist_ok=True)
        with _file_lock(path):
            state = self._read(name)
            before = json.dumps(state, sort_keys=True)
            yield state
            if json.dumps(state, sort_keys=True) != before:
                self._write_file(path, state)

    def _write_file(self, path: Path, data: Any) -> None:
        self._write_text(path, json.dumps(data, ensure_ascii=False))

    @staticmethod
    def _write_text(path: Path, text: str) -> None:
        handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".conduit-pc-")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                file.write(text)
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return None


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """Serializes the gateway's and the dashboard's writers of one file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(f".{path.name}.lock"), "a+b") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:
            handle.seek(0)
            for attempt in range(6):  # LK_LOCK gives up after about ten seconds
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
