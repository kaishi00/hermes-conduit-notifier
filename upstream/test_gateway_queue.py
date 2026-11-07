"""The session authority's message queue, in Conduit's shapes (Webapp queue support).

On the gateway's own listener every send joins the chat's queue (its FIFO):
a send into a running turn waits as ``queued``, and the chat's ``pending``
list (in a resume and in ``session.info`` pushes) is what Conduit draws its
queued messages and lost turns from. A queued message can be withdrawn
(``prompt.cancel``), edited (``prompt.replace_queued``) or sent into the running
turn (``prompt.steer_queued``). A turn a gateway restart cut off is ``unknown``
and holds every later message until it is discarded
(``prompt.resolve_unknown``), which Conduit's card does, and which the plugin
does itself for its own chats' turns (gateway_chats._settle_lost).

These start a gateway of their own, since they kill and restart it.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import threading
import time
import urllib.request
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from conftest import COPY_IGNORE, PLUGIN, READY_TIMEOUT_S, ROOT, _free_port, _hermes_bin
from test_gateway_listener import DRIVER, ConduitSocket, Listener, _create, _FakeModel, _refusal, _resume, _submit


class Gateway(Listener):
    """A listener this module may kill (as a crash or a power cut would) and start again."""

    def __init__(self, port: int, home: Path, env: Dict[str, str], log: Any) -> None:
        super().__init__(port, home, None)
        self.env = env
        self.log = log
        self.proc: Optional[subprocess.Popen] = None

    def start(self) -> None:
        self.proc = subprocess.Popen([_hermes_bin(), "gateway", "run"], env=self.env, stdout=self.log,
                                     stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + READY_TIMEOUT_S
        while True:
            if self.proc.poll() is not None:
                raise RuntimeError(f"the gateway exited ({self.proc.returncode}):\n{self._tail()}")
            if time.monotonic() > deadline:
                raise RuntimeError(f"the gateway's listener did not start:\n{self._tail()}")
            if (self.home / "webapp" / "session-token").exists():
                with contextlib.suppress(OSError):
                    with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/status", timeout=5) as resp:
                        if resp.status == 200:
                            return
            time.sleep(0.5)

    def kill(self) -> None:
        if self.proc is None:
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.proc.pid, signal.SIGKILL)
        self.proc.wait(30)
        self.proc = None

    def stop(self) -> None:
        if self.proc is None:
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.proc.pid, signal.SIGTERM)
        try:
            self.proc.wait(20)
        except subprocess.TimeoutExpired:
            self.kill()
        self.proc = None

    def _tail(self) -> str:
        return (self.home / "gateway.log").read_text()[-4000:]


@pytest.fixture(scope="module")
def gateway(tmp_path_factory: pytest.TempPathFactory):
    if importlib.util.find_spec("gateway.webapp_listener") is None:
        pytest.skip("this Hermes has no Webapp listener (before 2026-10-25)")
    home = tmp_path_factory.mktemp("hermes-queue-home")
    model = ThreadingHTTPServer(("127.0.0.1", 0), _FakeModel)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    port = _free_port()
    shutil.copytree(ROOT, home / "plugins" / PLUGIN, ignore=COPY_IGNORE)
    shutil.copytree(Path(__file__).parent / "listener_driver", home / "plugins" / DRIVER,
                    ignore=shutil.ignore_patterns("__pycache__"))
    (home / "config.yaml").write_text(yaml.safe_dump({
        "plugins": {"enabled": [PLUGIN, DRIVER]},
        "webapp": {"enabled": True, "port": port},
        "model": {"default": "fake-model", "provider": "custom",
                  "base_url": f"http://127.0.0.1:{model.server_address[1]}/v1", "api_key": "fake"},
    }))
    log = (home / "gateway.log").open("a")
    gw = Gateway(port, home, {**os.environ, "HERMES_HOME": str(home), "NO_COLOR": "1"}, log)
    try:
        gw.start()
        yield gw
    finally:
        gw.stop()
        log.close()
        model.shutdown()
        model.server_close()
        if os.environ.get("UPSTREAM_CONTRACT_LOG"):
            print((home / "gateway.log").read_text()[-8000:])


async def _pending(sock: ConduitSocket, sid: str) -> List[Dict[str, Any]]:
    """The chat's queue as a resume lists it (Conduit's resume, no rows)."""
    snapshot = await sock.result("session.resume", _resume(sid, omit_messages=True))
    assert isinstance(snapshot.get("pending"), list), sorted(snapshot)
    return snapshot["pending"]


async def _queued_behind(sock: ConduitSocket, sid: str, text: str) -> Dict[str, Any]:
    """A send into the running turn: the receipt, then its row in the queue."""
    params = _submit(sid, text)
    receipt = await sock.result("prompt.submit", params)
    assert receipt["status"] == "queued" and receipt["admission_id"], receipt
    row = next(r for r in await _pending(sock, sid) if r["admission_id"] == receipt["admission_id"])
    # Its row names the send by Conduit's own submission id, and says what may be done with it.
    assert row["input_id"] == params["submission_id"] and row["text"] == text, row
    assert row["status"] == "queued" and set(row["controls"]) == {"steer", "replace"}, row
    return row


async def _until(check, what: str, timeout: float = 60) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        found = await check()
        if found:
            return found
        assert time.monotonic() < deadline, f"{what} never happened"
        await asyncio.sleep(0.5)


async def _user_rows(sock: ConduitSocket, sid: str) -> List[str]:
    full = await sock.result("session.resume", _resume(sid, omit_messages=False))
    return [m.get("text") or m.get("content") for m in full["messages"] if m.get("role") == "user"]


def test_queued_messages_in_conduits_shapes(gateway):
    async def script(sock: ConduitSocket) -> None:
        sid = (await sock.result("session.create", _create()))["session_id"]
        await sock.result("prompt.submit", _submit(sid, "first"))
        start = await sock.event(sid, "message.start")
        # The turn's start names the admission it runs, which the queue lists.
        assert isinstance(start.get("admission_id"), str), start
        second = await _queued_behind(sock, sid, "second")
        third = await _queued_behind(sock, sid, "third")

        # Every change to the queue is pushed to the chat's viewers as session.info.
        pushed = [e["payload"]["pending"] for e in sock.events
                  if e.get("type") == "session.info" and e.get("session_id") == sid
                  and isinstance((e.get("payload") or {}).get("pending"), list)]
        assert any(third["admission_id"] in [r["admission_id"] for r in rows] for rows in pushed), pushed

        # Edit: the edited words take a new admission at the back of the queue.
        edited = await sock.result("prompt.replace_queued", {
            "session_id": sid, "admission_id": second["admission_id"],
            "submission_id": str(uuid.uuid4()), "text": "second, edited"})
        assert edited["status"] == "queued" and edited["admission_id"] != second["admission_id"], edited
        # Cancel: settled, never run.
        cancelled = await sock.result("prompt.cancel", {"session_id": sid, "admission_id": third["admission_id"]})
        assert (cancelled["status"], cancelled["outcome"]) == ("terminal", "cancelled"), cancelled
        rows = await _pending(sock, sid)
        assert [r["text"] for r in rows if r["status"] == "queued"] == ["second, edited"], rows

        # Send now: the queued words go into the running turn, and never run as their own.
        # Only once the turn's agent is up: before that Hermes refuses and keeps it queued.
        await sock.event(sid, "message.delta")
        steered = await sock.result("prompt.steer_queued", {"session_id": sid,
                                                             "admission_id": edited["admission_id"]})
        assert steered["status"] == "started", steered
        await _until(lambda: _quiet(sock, sid), "the chat going quiet")
        assert not [r for r in await _pending(sock, sid) if r["status"] == "queued"]
        users = await _user_rows(sock, sid)
        assert users[0] == "first" and "third" not in users and "second" not in users, users

        # A queued message that started meanwhile can't be sent into a turn any more.
        refused = await sock.call("prompt.steer_queued", {"session_id": sid,
                                                           "admission_id": edited["admission_id"]})
        assert "error" in refused, refused

    gateway.run(script)


async def _quiet(sock: ConduitSocket, sid: str) -> bool:
    snapshot = await sock.result("session.resume", _resume(sid, omit_messages=True))
    return snapshot.get("running") is False and not snapshot.get("pending")


def test_a_turn_a_restart_cut_off_holds_the_chat_until_discarded(gateway):
    out: Dict[str, Any] = {}

    async def before(sock: ConduitSocket) -> None:
        sid = (await sock.result("session.create", _create()))["session_id"]
        out["sid"] = sid
        await sock.result("prompt.submit", _submit(sid, "cut off"))
        await sock.event(sid, "message.start")
        out["waiting"] = (await _queued_behind(sock, sid, "waiting"))["admission_id"]

    gateway.run(before)
    gateway.kill()
    gateway.start()

    async def after(sock: ConduitSocket) -> None:
        sid = out["sid"]
        rows = await _pending(sock, sid)
        lost = [r for r in rows if r["status"] == "unknown"]
        assert [r["text"] for r in lost] == ["cut off"] and isinstance(lost[0]["execution_generation"], int), rows
        # Nothing runs, and a new send waits behind the lost turn with the one queued before.
        sent = await sock.result("prompt.submit", _submit(sid, "after the restart"))
        assert sent["status"] == "queued", sent
        await asyncio.sleep(5)
        snapshot = await sock.result("session.resume", _resume(sid, omit_messages=True))
        assert snapshot["running"] is False, snapshot
        assert [r["text"] for r in snapshot["pending"]] == ["cut off", "waiting", "after the restart"], snapshot

        # Discard names the lost turn by the generation it ran as.
        missing = await sock.call("prompt.resolve_unknown", {"session_id": sid, "admission_id": lost[0]["admission_id"]})
        assert _refusal(missing) == "invalid_params", missing
        discarded = await sock.result("prompt.resolve_unknown", {
            "session_id": sid, "admission_id": lost[0]["admission_id"],
            "execution_generation": lost[0]["execution_generation"]})
        assert (discarded["status"], discarded["outcome"]) == ("terminal", "interrupted"), discarded
        # Never resent: the ones behind it run, in order.
        users = await _until(lambda: _ran(sock, sid, "after the restart"), "the held sends running")
        assert users.count("cut off") <= 1 and users.index("waiting") < users.index("after the restart"), users

    gateway.run(after)


async def _ran(sock: ConduitSocket, sid: str, text: str) -> Optional[List[str]]:
    if not await _quiet(sock, sid):
        return None
    users = await _user_rows(sock, sid)
    return users if text in users else None


def test_the_plugins_own_lost_turn_is_discarded_by_its_next_send(gateway):
    # A Watch job's turn cut off by a restart (the plugin's session API sent it).
    status, out = gateway.plugin_call("session-api-start", plugin=DRIVER, timeout=180)
    assert status == 200 and out.get("session_id"), out
    gateway.kill()
    gateway.start()
    sid = out["session_id"]

    async def lost(sock: ConduitSocket) -> List[Dict[str, Any]]:
        return [r for r in await _pending(sock, sid) if r["status"] == "unknown"]
    rows = gateway.run(lost)
    assert len(rows) == 1 and rows[0]["input_id"].startswith("conduit-plugin-"), rows

    # Pulse's next turn in that chat discards it first, so it runs.
    status, sent = gateway.plugin_call(f"pulse-send?chat_id={sid}", plugin=DRIVER, timeout=180)
    assert status == 200 and sent["ok"] is True, sent

    async def ran(sock: ConduitSocket) -> List[str]:
        return await _until(lambda: _ran(sock, sid, "PULSE check in"), "Pulse's turn running")
    assert "PULSE check in" in gateway.run(ran)


def test_a_persons_lost_turn_is_left_for_them(gateway):
    out: Dict[str, Any] = {}

    async def before(sock: ConduitSocket) -> None:
        sid = (await sock.result("session.create", _create()))["session_id"]
        out["sid"] = sid
        await sock.result("prompt.submit", _submit(sid, "mine"))
        await sock.event(sid, "message.start")

    gateway.run(before)
    gateway.kill()
    gateway.start()
    status, sent = gateway.plugin_call(f"pulse-send?chat_id={out['sid']}", plugin=DRIVER, timeout=180)
    assert status == 200 and sent["ok"] is True, sent

    async def after(sock: ConduitSocket) -> None:
        rows = await _pending(sock, out["sid"])
        # The person's turn is still theirs to discard or resend; Pulse's waits behind it.
        assert [(r["text"], r["status"]) for r in rows][0] == ("mine", "unknown"), rows
        assert any(r["status"] == "queued" for r in rows), rows

    gateway.run(after)
