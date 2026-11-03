"""A cron job delivered to a Bot Chat on a real Hermes (cron_bot_chat).

Its own dashboard with a fake model, as test_bot_work's. The Bot Chat is made
on the app's socket, then two jobs with ``deliver: bot-chat`` run from the
dashboard's Run now: one the user lets the bot act on, then one that just
shows its report, and then the user writes in the Bot Chat. The test checks
the Bot Chat's own rows in state.db and the model's requests: the report that
just shows is a note in the chat with no turn (the model never sees Hermes'
cron wrapper over it), and the bot has it on the user's next turn; the other
is Hermes' own turn. A Hermes without the local-chat note (v0.22.5) skips:
every job keeps its turn there.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

import pytest

from conftest import COPY_IGNORE, PLUGIN, ROOT, TOKEN, _free_port, _hermes_bin
from test_bot_work import _fake_llm, _text_of, _write_profile

HEADER = 'report: shown in the chat, no reply asked for]'
WAIT_S = 120


def _shows_reports() -> bool:
    hermes_dir = Path(_hermes_bin()).resolve().parents[2]
    source = hermes_dir / "gateway" / "session_cron.py"
    return source.is_file() and "def deliver_to_local_chat" in source.read_text()


def _responder(fake: Any):
    def respond(record: Dict[str, Any]) -> Any:
        messages = record["body"].get("messages") or []
        user = next((_text_of(m) for m in reversed(messages) if m.get("role") == "user"), "")
        if "CRON-TASK" in user:
            return fake.Text(f"REPORT-{user.split('CRON-TASK-')[-1][:4]}: rain at nine")
        if "[Cronjob" in user:
            return fake.Text("BOT-ACTED on the report")
        return fake.Text("plain answer")
    return respond


def _http(port: int, method: str, path: str, body: Any = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                                     headers={"X-Hermes-Session-Token": TOKEN, "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=WAIT_S) as response:
        return json.loads(response.read() or b"null")


async def _socket(port: int, text: str = "", stored: str = "") -> str:
    """The Bot Chat on the app's socket: made (stored empty) or resumed,
    with one turn of the user's when there's text. Its stored id."""
    from websockets.asyncio.client import connect

    async with connect(f"ws://127.0.0.1:{port}/api/ws?token={TOKEN}", open_timeout=30, max_size=None) as ws:
        async def call(key: str, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": key, "method": method, "params": params}))
            while True:
                frame = json.loads(await asyncio.wait_for(ws.recv(), 60))
                if frame.get("id") == key:
                    return frame.get("result") or {}

        if stored:
            opened = await call("open", "session.resume", {"cols": 96, "session_id": stored})
        else:
            opened = await call("open", "session.create", {"cols": 96, "source": "desktop", "title": "Bot Chat"})
        if text:
            await call("turn", "prompt.submit", {"session_id": opened.get("session_id", ""), "text": text})
            deadline = time.monotonic() + WAIT_S
            while time.monotonic() < deadline:
                frame = json.loads(await asyncio.wait_for(ws.recv(), WAIT_S))
                if frame.get("method") == "event" and (frame.get("params") or {}).get("type") == "message.complete":
                    break
        return stored or opened.get("stored_session_id", "")


def _rows(home: Path, title: str = "Bot Chat") -> List[Dict[str, Any]]:
    con = sqlite3.connect(home / "state.db")
    try:
        ids = [row[0] for row in con.execute("SELECT id FROM sessions WHERE title = ?", (title,))]
        columns = [row[1] for row in con.execute("PRAGMA table_info(messages)")]
        wanted = [c for c in ("role", "content", "display_kind") if c in columns]
        return [dict(zip(wanted, row)) for row in con.execute(
            f"SELECT {', '.join(wanted)} FROM messages WHERE session_id IN ({','.join('?' * len(ids))}) ORDER BY id",
            ids)] if ids else []
    finally:
        con.close()


def _holding(home: Path, text: str) -> str:
    """The Bot Chat session with a row that says this."""
    con = sqlite3.connect(home / "state.db")
    try:
        row = con.execute("SELECT m.session_id FROM messages m JOIN sessions s ON s.id = m.session_id "
                          "WHERE s.title = 'Bot Chat' AND m.content LIKE ? ORDER BY m.id DESC LIMIT 1",
                          (f"%{text}%",)).fetchone()
        return row[0] if row else ""
    finally:
        con.close()


@pytest.fixture(scope="module")
def ran(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, Any]:
    if not _shows_reports():
        pytest.skip("this Hermes can't write a cron result into a chat as a note")
    fake = _fake_llm()
    hermes = _hermes_bin()
    tmp = tmp_path_factory.mktemp("cron-bot-chat")
    home = tmp / "home"
    home.mkdir()
    env = {**os.environ, "HERMES_HOME": str(home), "HERMES_DASHBOARD_SESSION_TOKEN": TOKEN, "NO_COLOR": "1"}
    shutil.copytree(ROOT, home / "plugins" / PLUGIN, ignore=COPY_IGNORE)
    server = fake.FakeLLMServer(_responder(fake), default_text="default")
    server.start()
    _write_profile(home, server.base_url, "launch", "")
    port = _free_port()
    log_path = tmp / "dashboard.log"
    result: Dict[str, Any] = {"home": home, "server": server, "log": log_path}
    with log_path.open("w") as log:
        proc = subprocess.Popen([hermes, "dashboard", "--no-open", "--skip-build", "--port", str(port)],
                                env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            deadline = time.monotonic() + 150
            while True:
                if proc.poll() is not None:
                    raise RuntimeError(f"the dashboard exited ({proc.returncode}):\n{log_path.read_text()[-4000:]}")
                if time.monotonic() > deadline:
                    raise RuntimeError(f"the dashboard did not start:\n{log_path.read_text()[-4000:]}")
                with contextlib.suppress(OSError):
                    if _http(port, "GET", "/api/health") is not None:
                        break
                time.sleep(0.5)
            result["chat"] = asyncio.run(_socket(port))
            # The bot acts first, before the user's turn on the socket (whose
            # session then holds the chat's writer, and Hermes' own delivery
            # waits for it): its door then holds the chat as a local session.
            for key, act in (("acted", True), ("shown", False)):
                job = _http(port, "POST", "/api/cron/jobs", {
                    "name": f"Probe {key}", "prompt": f"CRON-TASK-{key}", "schedule": "every 24h",
                    "deliver": "bot-chat"})
                job_id = job.get("id") or (job.get("job") or {}).get("id")
                if act:
                    _http(port, "PUT", f"/api/plugins/{PLUGIN}/cron/notifications", {"job_id": job_id, "act": True})
                result[f"{key}_choices"] = _http(port, "GET", f"/api/plugins/{PLUGIN}/cron/notifications")
                _http(port, "POST", f"/api/cron/jobs/{job_id}/trigger")
                deadline = time.monotonic() + WAIT_S
                marker = f"REPORT-{key[:4]}"
                while time.monotonic() < deadline:
                    rows = _rows(home)
                    if any(marker in _text_of(row) for row in rows) and (
                            not act or any("BOT-ACTED" in _text_of(row) for row in rows)):
                        break
                    time.sleep(0.5)
                if not act:
                    # The user's next turn: the bot has the report as context.
                    # In the Bot Chat as it is now: Hermes' door may have moved
                    # it to a session of its own when the bot acted.
                    asyncio.run(_socket(port, "AFTER-REPORT what did it say?", _holding(home, "REPORT-show")))
                    result["after"] = [record["body"] for record in server.requests
                                       if "AFTER-REPORT" in json.dumps(record["body"])]
            time.sleep(1)
            result["rows"] = _rows(home)
            result["requests"] = [record["body"] for record in server.requests]
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGTERM)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(20)
            # The gateway the dashboard started runs on in a session of its own.
            with contextlib.suppress(OSError, ValueError, TypeError):
                os.kill(int(json.loads((home / "gateway.lock").read_text())["pid"]), signal.SIGTERM)
            server.stop()
            if os.environ.get("UPSTREAM_CONTRACT_LOG"):
                print(log_path.read_text()[-8000:])
    return result


def _users(requests: List[Dict[str, Any]]) -> List[str]:
    return [_text_of(m) for body in requests for m in body.get("messages") or [] if m.get("role") == "user"]


def test_the_route_says_this_hermes_shows_reports(ran: Dict[str, Any]) -> None:
    assert ran["shown_choices"]["shows_reports"] is True
    assert ran["acted_choices"]["act"]


def test_a_report_shows_in_the_bot_chat_as_a_note_without_a_turn(ran: Dict[str, Any]) -> None:
    notes = [row for row in ran["rows"] if "REPORT-show" in _text_of(row)]
    assert notes, f"no report in the Bot Chat: {ran['rows']}"
    note = notes[0]
    assert _text_of(note).startswith('[Cron job "Probe shown" ' + HEADER)
    assert note.get("display_kind"), note
    # The model never had a Bot Chat turn over it.
    assert not any("[Cronjob" in text and "REPORT-show" in text for text in _users(ran["requests"]))
    assert not any(row.get("role") == "assistant" and "BOT-ACTED" in _text_of(row)
                   for row in ran["rows"][ran["rows"].index(note):ran["rows"].index(note) + 2])


def test_the_bot_has_the_report_on_the_users_next_turn(ran: Dict[str, Any]) -> None:
    assert ran["after"], "the user's next turn never reached the model"
    # The turn's own request, not the chat titler's beside it.
    turn = max(ran["after"], key=lambda body: len(body.get("messages") or []))
    assert "REPORT-show" in json.dumps(turn.get("messages"))


def test_a_job_that_lets_the_bot_act_keeps_hermes_turn(ran: Dict[str, Any]) -> None:
    assert any("[Cronjob" in text and "REPORT-acte" in text for text in _users(ran["requests"])), _users(ran["requests"])
    assert any(row.get("role") == "assistant" and "BOT-ACTED" in _text_of(row) for row in ran["rows"])
    assert not any(_text_of(row).startswith('[Cron job "Probe acted"') for row in ran["rows"])
