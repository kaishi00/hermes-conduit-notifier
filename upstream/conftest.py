"""The upstream contract harness: a real Hermes dashboard with this plugin.

The plugin suite in tests/ stubs Hermes, so it can only check our side of the
contract. These tests start the dashboard of a real Hermes install (see
install-hermes.sh) on localhost, with three profiles:

* the launch profile, which the dashboard starts with;
* ``second``, a secondary profile with the plugin enabled;
* ``disabled``, a secondary profile with the plugin installed but not enabled.

Each profile's speech server URL points at a fake speech server with the
profile's name as its path, so a socket test sees which profile's settings
the plugin used: a socket that opens in the wrong profile is a failure, not a
pass. No model, speech server or phone is needed.

Run with Hermes' own Python, from the repo root:

    HERMES_BIN=<hermes dir>/.venv/bin/hermes \\
        <hermes dir>/.venv/bin/python -m pytest --rootdir=upstream upstream

Known upstream failures (contract.yml, ``known_failures``) report as xfail
while their signature matches, and fail when the test passes, so a fix
upstream is noticed. UPSTREAM_CONTRACT_RAW=1 reports them as plain results.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = Path(__file__).resolve().parent / "contract.yml"
PLUGIN = "conduit_push"
TOKEN = "upstream-contract-session-token"
READY_TIMEOUT_S = 120
# What a copy of the plugin leaves out: the relay and mailbox are not plugin code.
COPY_IGNORE = shutil.ignore_patterns(".git", "node_modules", "relay", "mailbox", "tests", "upstream",
                                     "__pycache__", ".pytest_cache")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _hermes_bin() -> str:
    found = os.environ.get("HERMES_BIN") or shutil.which("hermes")
    if not found:
        pytest.exit("HERMES_BIN is not set and no hermes is on PATH (see upstream/install-hermes.sh)", returncode=2)
    return found


class FakeSpeechServer:
    """A WebSocket server that records each path it is dialed on and says it is ready."""

    def __init__(self) -> None:
        self.port = _free_port()
        self._paths: List[str] = []
        self._paths_lock = threading.Lock()
        self._loop = asyncio.new_event_loop()
        self._started = threading.Event()
        self._stop: Optional[asyncio.Future] = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        from websockets.asyncio.server import serve

        async def handler(ws: Any) -> None:
            with self._paths_lock:
                self._paths.append(ws.request.path)
            await ws.send(json.dumps({"type": "ready"}))
            with contextlib.suppress(Exception):
                await ws.wait_closed()

        async def main() -> None:
            self._stop = self._loop.create_future()
            async with serve(handler, "127.0.0.1", self.port):
                self._started.set()
                await self._stop

        self._loop.run_until_complete(main())

    def start(self) -> None:
        self._thread.start()
        if not self._started.wait(10):
            raise RuntimeError("the fake speech server did not start")

    def stop(self) -> None:
        if self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set_result, None)
        self._thread.join(5)
        if not self._thread.is_alive():
            self._loop.close()

    @property
    def paths(self) -> List[str]:
        """The paths dialed so far, in order."""
        with self._paths_lock:
            return list(self._paths)

    def url(self, profile: str) -> str:
        return f"ws://127.0.0.1:{self.port}/{profile}"


class Dashboard:
    """The running dashboard: HTTP and WebSocket helpers for the plugin's routes."""

    def __init__(self, port: int, speech: FakeSpeechServer, home: Path) -> None:
        self.port = port
        self.speech = speech
        self.home = home

    def plugin_url(self, path: str, scheme: str = "http", token: bool = False, **query: str) -> str:
        if token:
            query = {"token": TOKEN, **query}
        qs = f"?{urllib.parse.urlencode(query)}" if query else ""
        return f"{scheme}://127.0.0.1:{self.port}/api/plugins/{PLUGIN}/{path.lstrip('/')}{qs}"

    def get(self, path: str, auth: bool = True, **query: str) -> Tuple[int, Any]:
        return self.request("GET", path, auth=auth, **query)

    def request(self, method: str, path: str, auth: bool = True, body: Any = None,
                **query: str) -> Tuple[int, Any]:
        headers = {"X-Hermes-Session-Token": TOKEN} if auth else {}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.plugin_url(path, **query), data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
        try:
            return status, json.loads(raw or b"null")
        except ValueError:
            return status, raw.decode(errors="replace")

    def open_socket(self, path: str, token: bool = True, **query: str) -> "SocketResult":
        """Opens a plugin socket, waits for the speech server's first event, and reports what happened."""
        return asyncio.run(self._open_socket(path, token, query))

    async def _open_socket(self, path: str, token: bool, query: Dict[str, str]) -> "SocketResult":
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

        before = len(self.speech.paths)
        url = self.plugin_url(path, scheme="ws", token=token, **query)
        try:
            async with connect(url, open_timeout=15) as ws:
                try:
                    first = await asyncio.wait_for(ws.recv(), 15)
                    result = SocketResult(opened=True, first=str(first))
                except ConnectionClosed as exc:
                    rcvd = exc.rcvd
                    result = SocketResult(opened=True, close_code=rcvd.code if rcvd else None,
                                          close_reason=rcvd.reason if rcvd else "")
                except asyncio.TimeoutError:
                    result = SocketResult(opened=True)
        except InvalidStatus as exc:
            result = SocketResult(opened=False, http_status=exc.response.status_code)
        except (OSError, asyncio.TimeoutError, InvalidHandshake) as exc:
            # The handshake itself failed: a route's answer, not the harness's.
            result = SocketResult(opened=False, error=type(exc).__name__)
        # The fake server records a dial before it sends "ready", so once the first event
        # arrived or the socket was refused, the list is final. Only a socket that opened
        # and then went quiet could still be dialing: give it time.
        deadline = time.monotonic() + 5
        while (result.opened and result.first is None and result.close_code is None
               and len(self.speech.paths) == before and time.monotonic() < deadline):
            time.sleep(0.05)
        result.dialed = self.speech.paths[before:]
        return result


class SocketResult:
    def __init__(self, opened: bool, first: Optional[str] = None, close_code: Optional[int] = None,
                 close_reason: str = "", http_status: Optional[int] = None, error: Optional[str] = None) -> None:
        self.opened = opened
        self.first = first
        self.close_code = close_code
        self.close_reason = close_reason
        self.http_status = http_status
        self.error = error
        self.dialed: List[str] = []

    @property
    def refused(self) -> bool:
        """Refused by an HTTP status at the handshake or a close before any speech server event."""
        if self.first is not None:
            return False
        return (not self.opened and self.http_status is not None) or self.close_code is not None

    @property
    def signature(self) -> str:
        """A stable description of the outcome, for known failures and issue keys."""
        if not self.opened:
            return f"HTTP {self.http_status}" if self.http_status is not None else f"handshake {self.error}"
        if self.close_code is not None:
            return f"close {self.close_code}"
        return f"dialed {','.join(self.dialed) or 'nothing'}"

    def __repr__(self) -> str:
        return (f"SocketResult(opened={self.opened}, http_status={self.http_status}, error={self.error}, close={self.close_code} "
                f"{self.close_reason!r}, dialed={self.dialed}, first={self.first!r})")


def _set_config(path: Path, enabled: bool, speech_url: str) -> None:
    config = yaml.safe_load(path.read_text()) if path.exists() else None
    config = config or {}
    plugins = config.setdefault("plugins", {})
    names = [n for n in (plugins.get("enabled") or []) if n != PLUGIN]
    plugins["enabled"] = names + ([PLUGIN] if enabled else [])
    config.setdefault("voice", {}).setdefault("speech_server", {})["url"] = speech_url
    path.write_text(yaml.safe_dump(config))


@pytest.fixture(scope="session")
def dashboard(tmp_path_factory: pytest.TempPathFactory):
    hermes = _hermes_bin()
    home = tmp_path_factory.mktemp("hermes-home")
    env = {**os.environ, "HERMES_HOME": str(home), "HERMES_DASHBOARD_SESSION_TOKEN": TOKEN, "NO_COLOR": "1"}
    for name in ("second", "disabled"):
        subprocess.run([hermes, "profile", "create", name], env=env, check=True, capture_output=True, timeout=120)

    speech = FakeSpeechServer()
    log_path = home / "dashboard.log"
    log = log_path.open("w")
    proc: Optional[subprocess.Popen] = None
    try:
        speech.start()
        shutil.copytree(ROOT, home / "plugins" / PLUGIN, ignore=COPY_IGNORE)
        _set_config(home / "config.yaml", True, speech.url("launch"))
        for name in ("second", "disabled"):
            profile = home / "profiles" / name
            (profile / "plugins").mkdir(exist_ok=True)
            _set_config(profile / "config.yaml", name != "disabled", speech.url(name))

        port = _free_port()
        proc = subprocess.Popen([hermes, "dashboard", "--no-open", "--skip-build", "--port", str(port)],
                                env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        board = Dashboard(port, speech, home)
        deadline = time.monotonic() + READY_TIMEOUT_S
        while True:
            if proc.poll() is not None:
                raise RuntimeError(f"the dashboard exited ({proc.returncode}):\n{log_path.read_text()[-4000:]}")
            if time.monotonic() > deadline:
                raise RuntimeError(f"the dashboard did not start:\n{log_path.read_text()[-4000:]}")
            try:
                status, _ = board.get("capabilities")
                if status == 200:
                    break
            except OSError:
                pass
            time.sleep(0.5)
        yield board
    finally:
        if proc is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(15)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                # Best effort: a teardown error would hide the tests' own result.
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(15)
        log.close()
        speech.stop()
        if os.environ.get("UPSTREAM_CONTRACT_LOG"):
            print(log_path.read_text()[-8000:])


def _known_failures() -> List[Dict[str, Any]]:
    data = yaml.safe_load(CONTRACT.read_text()) or {}
    return list(data.get("known_failures") or [])


@pytest.fixture
def known_failure(request: pytest.FixtureRequest):
    """``known_failure(passed, signature)`` ends a test that checks an upstream behavior.

    A test listed in contract.yml's known_failures is xfailed while it fails
    with the listed signature; it fails if it passes (upstream fixed it: the
    workaround and the entry can go) or fails differently (a new break).
    """
    name = request.node.originalname or request.node.name
    entry = next((e for e in _known_failures() if e.get("test") == name), None)
    raw = os.environ.get("UPSTREAM_CONTRACT_RAW") == "1"

    def check(passed: bool, signature: str, detail: str = "") -> None:
        if entry is None or raw:
            assert passed, f"{signature}: {detail}"
            return
        if passed:
            pytest.fail(f"Known failure no longer fails: upstream fixed {entry.get('issue')}. "
                        f"Remove its known_failures entry and review the workaround ({entry.get('workaround')}).")
        if signature != entry.get("signature"):
            pytest.fail(f"Known failure {entry.get('issue')} now fails differently: {signature} "
                        f"(was {entry.get('signature')}): {detail}")
        pytest.xfail(f"known upstream limitation {entry.get('issue')}: {signature}")

    return check
