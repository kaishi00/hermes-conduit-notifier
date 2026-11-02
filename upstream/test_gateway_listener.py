"""Conduit on the gateway's own listener (hermes-conduit#1618).

Hermes serves /api/ws twice: ``hermes dashboard`` answers session verbs with
its TUI gateway, and the gateway's own listener (the Webapp, ``hermes
webapp``) with its session authority, which takes other shapes for the same
verbs. Conduit tells them apart by the ``session_authority`` flag in
``gateway.ready`` and sends each its own shape. These tests start a real
gateway with the Webapp listener and send the frames Conduit sends there
(HermesClient.swift, "The session authority's shapes"), signed in the way
Conduit signs in: with the Webapp's session token, like the browser.

The other half is the resend: a request Conduit built before it read
``gateway.ready`` goes in the dashboard's shape and is sent again in the
authority's when refused. That is only safe while the authority refuses the
dashboard's shape before anything runs, so that is checked too.

A Hermes release without the Webapp (before 2026-10-25) skips these; a
canary without it fails, since that is the break this leg is here to catch. A small fake
model server answers the turns, slowly enough that a steer finds one running.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from conftest import COPY_IGNORE, PLUGIN, READY_TIMEOUT_S, ROOT, FakeSpeechServer, _free_port, _hermes_bin

WORDS = 10
WORD_DELAY_S = 0.4
DRIVER = "conduit_listener_driver"


class _FakeModel(BaseHTTPRequestHandler):
    """An OpenAI-compatible model that streams WORDS words, one every WORD_DELAY_S.

    A user message with RUN in it gets a terminal call that needs approval,
    and one with ASK a clarify question, so a turn waits on a prompt. The
    reply reports its usage, so a model switch has a context to guard.
    """

    # Every request's model and messages, for what a turn was told.
    seen: List[Dict[str, Any]] = []

    def log_message(self, *args: Any) -> None:
        pass

    def _json(self, body: Dict[str, Any]) -> None:
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        self._json({"data": [{"id": "fake-model", "object": "model"}]})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        request = json.loads(self.rfile.read(length) or b"{}")
        _FakeModel.seen.append({"model": request.get("model"), "messages": request.get("messages") or []})
        # A prediction's side question gets the JSON it asks for (predict.py).
        asked = json.dumps(request.get("messages") or [])
        reply = '{"replies":[{"text":"sounds good","p":0.9}]}' if "Predict what the user" in asked else "ok"
        if not request.get("stream") or reply != "ok":
            if request.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                body = {"id": "x", "object": "chat.completion.chunk", "created": 0, "model": "fake-model",
                        "choices": [{"index": 0, "delta": {"content": reply}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    self.wfile.write(f"data: {json.dumps(body)}\n\n".encode() + b"data: [DONE]\n\n")
                return
            self._json({"id": "x", "object": "chat.completion", "created": 0, "model": "fake-model",
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": reply},
                                     "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def chunk(delta: Dict[str, Any], finish: Optional[str] = None) -> bytes:
            body = {"id": "x", "object": "chat.completion.chunk", "created": 0, "model": "fake-model",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            return f"data: {json.dumps(body)}\n\n".encode()

        messages = request.get("messages") or []
        last = messages[-1] if messages else {}
        text = last.get("content") if isinstance(last.get("content"), str) else ""
        if last.get("role") == "user" and ("RUN" in text or "ASK" in text):
            if "ASK" in text:
                arguments = {"questions": [{"question": "Which one?", "choices": ["a", "b"]}]}
                call = {"index": 0, "id": "call_1", "type": "function",
                        "function": {"name": "clarify", "arguments": json.dumps(arguments)}}
            else:
                call = {"index": 0, "id": "call_1", "type": "function",
                        "function": {"name": "terminal", "arguments": json.dumps({"command": "rm -rf /tmp/conduit-contract-nothing"})}}
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(chunk({"tool_calls": [call]}) + chunk({}, "tool_calls") + b"data: [DONE]\n\n")
            return
        try:
            for index in range(WORDS):
                self.wfile.write(chunk({"content": f"word{index} "}))
                self.wfile.flush()
                time.sleep(WORD_DELAY_S)
            end = {"id": "x", "object": "chat.completion.chunk", "created": 0, "model": "fake-model",
                   "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 5000, "completion_tokens": WORDS, "total_tokens": 5000 + WORDS}}
            self.wfile.write(f"data: {json.dumps(end)}\n\n".encode() + b"data: [DONE]\n\n")
        except (BrokenPipeError, ConnectionResetError):
            pass


class Listener:
    """The gateway's listener: Conduit's socket, signed in with the Webapp's session token."""

    def __init__(self, port: int, home: Path, speech: FakeSpeechServer) -> None:
        self.port = port
        self.home = home
        self.speech = speech

    @property
    def token(self) -> str:
        return (self.home / "webapp" / "session-token").read_text().strip()

    def plugin_get(self, path: str) -> int:
        return self.plugin_call(path)[0]

    def plugin_call(self, path: str, body: Optional[Dict[str, Any]] = None, plugin: str = PLUGIN,
                    timeout: float = 30) -> tuple:
        """``(status, JSON body)`` of a plugin route: a POST of ``body``, else a GET."""
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/plugins/{plugin}/{path}", data=data,
                                     headers={"X-Hermes-Session-Token": self.token,
                                              **({"Content-Type": "application/json"} if data else {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()[:2000].decode(errors="replace")

    def run(self, script) -> Any:
        """Runs ``await script(socket)`` on a fresh Conduit socket."""
        return asyncio.run(self._run(script))

    async def _run(self, script) -> Any:
        from websockets.asyncio.client import connect

        url = f"ws://127.0.0.1:{self.port}/api/ws?token={self.token}"
        async with connect(url, open_timeout=15, max_size=None, proxy=None) as ws:
            sock = ConduitSocket(ws)
            reader = asyncio.create_task(sock.read())
            try:
                await asyncio.wait_for(sock.ready.wait(), 20)
                return await script(sock)
            finally:
                reader.cancel()
                # read() keeps a dropped socket's error to the calls it fails, so the
                # script's own assertion is the failure reported.
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await reader


class ConduitSocket:
    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self.ready = asyncio.Event()
        self.ready_payload: Dict[str, Any] = {}
        self.events: List[Dict[str, Any]] = []
        self._pending: Dict[int, asyncio.Future] = {}
        self._next = 0
        self._event_seen = asyncio.Event()
        self.closed: Optional[str] = None

    async def read(self) -> None:
        from websockets.exceptions import ConnectionClosed

        reason = "the socket closed"
        try:
            async for raw in self.ws:
                try:
                    frame = json.loads(raw)
                except ValueError as exc:
                    # Named as what it is, not as a dropped socket.
                    reason = f"a frame that isn't JSON ({exc}): {str(raw)[:200]}"
                    break
                if not isinstance(frame, dict):
                    reason = f"a frame that isn't a JSON object: {str(raw)[:200]}"
                    break
                self._take(frame)
        except (ConnectionClosed, OSError) as exc:
            reason = f"the socket closed: {exc!r}"
        finally:
            # A clean close fails the waiting calls too, and wakes an event wait.
            self.closed = reason
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionError(reason))
            self._pending.clear()
            self._event_seen.set()

    def _take(self, frame: Dict[str, Any]) -> None:
        # A frame with a method is an event (or a request of the server's),
        # never the answer to one of ours, even when its id collides.
        if "method" not in frame and ("result" in frame or "error" in frame):
            future = self._pending.pop(frame.get("id"), None)
            if future is not None and not future.done():
                future.set_result(frame)
        elif frame.get("method") == "event":
            params = frame.get("params") or {}
            if params.get("type") == "gateway.ready":
                self.ready_payload = params.get("payload") or {}
                self.ready.set()
            self.events.append(params)
            self._event_seen.set()

    async def call(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """The whole answer frame: ``result`` or ``error``."""
        self._next += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[self._next] = future
        await self.ws.send(json.dumps({"jsonrpc": "2.0", "id": self._next, "method": method, "params": params}))
        return await asyncio.wait_for(future, 60)

    async def result(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        frame = await self.call(method, params)
        assert "error" not in frame, f"{method} {params} -> {frame['error']}"
        return frame["result"]

    async def event(self, session_id: str, kind: str, timeout: float = 60) -> Dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            # Cleared before the scan, so an event that lands during it still wakes the wait.
            self._event_seen.clear()
            for event in self.events:
                if event.get("type") == kind and event.get("session_id") == session_id:
                    return event
            assert self.closed is None, f"no {kind} for {session_id} before {self.closed}"
            remaining = deadline - time.monotonic()
            assert remaining > 0, f"no {kind} for {session_id}; saw {[e.get('type') for e in self.events]}"
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._event_seen.wait(), remaining)


def _refusal(frame: Dict[str, Any]) -> str:
    error = frame.get("error") or {}
    return (error.get("data") or {}).get("reason") or str(error.get("code"))


def _submit(session_id: str, text: str) -> Dict[str, Any]:
    """Conduit's prompt.submit on the authority: one submission id per message."""
    return {"session_id": session_id, "text": text, "submission_id": str(uuid.uuid4())}


def _create() -> Dict[str, Any]:
    """Conduit's session.create on the authority (HermesClient.createParams)."""
    return {"source": "gui", "reasoning": "low"}


def _resume(session_id: str, omit_messages: bool) -> Dict[str, Any]:
    """Conduit's session.resume on the authority: no inline_images."""
    params: Dict[str, Any] = {"session_id": session_id, "cols": 96, "source": "desktop"}
    if omit_messages:
        params["omit_messages"] = True
    return params


def _mutate(snapshot: Dict[str, Any], session_id: str, operation: str, payload: Dict[str, Any],
            fences_generation: bool) -> Dict[str, Any]:
    params = {"session_id": session_id, "request_id": str(uuid.uuid4()), "operation": operation,
              "payload": payload, "expected_revision": snapshot["revision"]}
    if fences_generation:
        params["expected_generation"] = snapshot["execution_generation"]
    return params


@pytest.fixture(scope="module")
def listener(tmp_path_factory: pytest.TempPathFactory):
    if importlib.util.find_spec("gateway.webapp_listener") is None:
        ref = os.environ.get("HERMES_REF", "")
        # Only a release tag (vX.Y.Z) or a local run may lack it: a canary, or a commit
        # given by hand, is newer than the Webapp, so a missing listener is the break.
        if ref and not re.fullmatch(r"v\d+\.\d+\.\d+", ref):
            pytest.fail(f"Hermes {ref} has no gateway.webapp_listener: Conduit's Webapp support needs a look")
        pytest.skip("this Hermes has no Webapp listener (before 2026-10-25)")
    hermes = _hermes_bin()
    home = tmp_path_factory.mktemp("hermes-gateway-home")
    model: Optional[ThreadingHTTPServer] = None
    speech: Optional[FakeSpeechServer] = None
    env = {**os.environ, "HERMES_HOME": str(home), "NO_COLOR": "1"}
    log_path = home / "gateway.log"
    log = None
    proc: Optional[subprocess.Popen] = None
    serving = False
    speaking = False
    try:
        model = ThreadingHTTPServer(("127.0.0.1", 0), _FakeModel)
        speech = FakeSpeechServer()
        port = _free_port()
        log = log_path.open("w")
        threading.Thread(target=model.serve_forever, daemon=True).start()
        serving = True
        speech.start()
        speaking = True
        shutil.copytree(ROOT, home / "plugins" / PLUGIN, ignore=COPY_IGNORE)
        # Test-only routes that drive the plugin's own chats as its Watch calls and Pulse do.
        shutil.copytree(Path(__file__).parent / "listener_driver", home / "plugins" / DRIVER,
                        ignore=shutil.ignore_patterns("__pycache__"))
        (home / "config.yaml").write_text(yaml.safe_dump({
            "plugins": {"enabled": [PLUGIN, DRIVER]},
            "webapp": {"enabled": True, "port": port},
            # A speech server to relay to, so a signed plugin socket shows it got past sign-in.
            "voice": {"speech_server": {"url": speech.url("listener")}},
            # Any switch on a chat with context asks first, so the confirm exchange can be tried.
            "model": {"default": "fake-model", "provider": "custom", "switch_context_confirm_tokens": 1,
                      "base_url": f"http://127.0.0.1:{model.server_address[1]}/v1", "api_key": "fake"},
        }))
        proc = subprocess.Popen([hermes, "gateway", "run"], env=env, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True)
        deadline = time.monotonic() + READY_TIMEOUT_S
        while True:
            if proc.poll() is not None:
                raise RuntimeError(f"the gateway exited ({proc.returncode}):\n{log_path.read_text()[-4000:]}")
            if time.monotonic() > deadline:
                raise RuntimeError(f"the gateway's listener did not start:\n{log_path.read_text()[-4000:]}")
            if (home / "webapp" / "session-token").exists():
                with contextlib.suppress(OSError):
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=5) as resp:
                        if resp.status == 200:
                            break
            time.sleep(0.5)
        yield Listener(port, home, speech)
    finally:
        if proc is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(20)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(15)
        if log is not None:
            log.close()
        if serving:
            model.shutdown()
        if model is not None:
            model.server_close()
        if speaking:
            speech.stop()
        if os.environ.get("UPSTREAM_CONTRACT_LOG"):
            print(log_path.read_text()[-8000:])


def test_ready_names_the_session_authority_and_the_plugin_is_served(listener):
    async def script(sock: ConduitSocket) -> Dict[str, Any]:
        return sock.ready_payload

    assert listener.run(script).get("session_authority") is True
    # The listener mounts the dashboard, so the plugin's capabilities route answers on it.
    assert listener.plugin_get("capabilities") == 200
    # And the speech-server socket keeps the dashboard's sign-in: the plugin closes one
    # without the session token with 4401, and relays one with it to the speech server.
    assert asyncio.run(_socket_close(listener, "speech-server/socket")) == 4401
    assert asyncio.run(_socket_first_frame(listener, "speech-server/socket")) == {"type": "ready"}
    assert listener.speech.paths == ["/listener"]


async def _socket_first_frame(listener: "Listener", path: str) -> Any:
    """The first frame a signed plugin socket brings back."""
    from websockets.asyncio.client import connect

    async with connect(f"ws://127.0.0.1:{listener.port}/api/plugins/{PLUGIN}/{path}?token={listener.token}",
                       open_timeout=15, proxy=None) as ws:
        return json.loads(await asyncio.wait_for(ws.recv(), 15))


async def _socket_close(listener: "Listener", path: str) -> Optional[int]:
    """How an unsigned plugin socket ended: its close code, or None while it stays open."""
    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosed

    # A missing route is refused at the handshake, which raises here.
    async with connect(f"ws://127.0.0.1:{listener.port}/api/plugins/{PLUGIN}/{path}",
                       open_timeout=15, proxy=None) as ws:
        try:
            # Whatever comes first, the close code is what tells, within 15 s overall.
            async with asyncio.timeout(15):
                while True:
                    await ws.recv()
        except ConnectionClosed:
            return ws.close_code
        except TimeoutError:
            return None


def test_a_turn_in_conduits_authority_shapes(listener):
    async def script(sock: ConduitSocket) -> None:
        created = await sock.result("session.create", _create())
        sid = created["session_id"]
        assert isinstance(created["revision"], int) and isinstance(created["execution_generation"], int)

        receipt = await sock.result("prompt.submit", _submit(sid, "first"))
        assert receipt["status"] in {"queued", "started"}, receipt
        start = await sock.event(sid, "message.start")
        generation = start["execution_generation"]

        # A steer names the running turn by the generation its events carry.
        steered = await sock.result("session.steer", {"session_id": sid, "execution_generation": generation,
                                                       "text": "keep it short"})
        assert steered["status"] == "queued", steered
        await sock.event(sid, "message.complete")

        # A turn that ended: the old generation is stale, and the refusal says so.
        stale = await sock.call("session.interrupt", {"session_id": sid, "execution_generation": generation - 1})
        assert _refusal(stale) == "stale_generation", stale

        await sock.result("prompt.submit", _submit(sid, "second"))
        # Done when the chat is idle with a reply after "second", however the steer ran.
        deadline = time.monotonic() + 60
        while True:
            full = await sock.result("session.resume", _resume(sid, omit_messages=False))
            roles = [(m.get("role"), m.get("text") or m.get("content")) for m in full["messages"]]
            asked = [i for i, (role, text) in enumerate(roles) if role == "user" and text == "second"]
            if full.get("running") is False and asked and any(r == "assistant" for r, _ in roles[asked[-1]:]):
                break
            assert time.monotonic() < deadline, f"the second turn never completed: {roles}"
            await asyncio.sleep(0.5)

        replies = [m for m in full["messages"] if m.get("role") == "assistant"]
        assert len(replies) >= 2 and all(isinstance(m.get("row_id"), int) for m in replies), full["messages"]
        through = replies[0]["row_id"]
        kept = sum(1 for m in full["messages"] if isinstance(m.get("row_id"), int) and m["row_id"] <= through)
        snapshot = await sock.result("session.resume", _resume(sid, omit_messages=True))
        assert snapshot["messages"] == [] and snapshot["running"] is False

        renamed = await sock.result("session.mutate", _mutate(snapshot, sid, "rename", {"title": "Trip plans"},
                                                               fences_generation=False))
        assert (await sock.result("session.info", {"session_id": sid})).get("title") == "Trip plans"
        snapshot = {**snapshot, "revision": renamed["revision"]}

        # A stale revision is refused with the current fences, which Conduit retries with once.
        conflict = await sock.call("session.mutate", _mutate({**snapshot, "revision": 0}, sid, "rename",
                                                             {"title": "x"}, fences_generation=False))
        assert _refusal(conflict) == "revision_conflict", conflict
        assert conflict["error"]["data"]["revision"] == renamed["revision"], conflict

        # Branching keeps the rows through the first reply.
        branched = await sock.result("session.mutate", _mutate(
            snapshot, sid, "branch", {"title": "Branch", "through_message_id": through},
            fences_generation=True))
        assert branched["branched_session_id"] and branched["branched_session_id"] != sid, branched
        assert branched.get("copied_messages") == kept, branched
        snapshot = {**snapshot, "revision": branched["revision"]}

        compressed = await sock.result("session.mutate", _mutate(snapshot, sid, "compress",
                                                                  {"focus": "names"}, fences_generation=True))
        # What Conduit's SessionCompressResult reads: a status and the summary's headline.
        assert compressed["operation"] == "compress" and compressed.get("status"), compressed
        assert isinstance((compressed.get("summary") or {}).get("headline"), str), compressed

        # A chat this short has nothing to compress, and Hermes says so: the verb ran and
        # left the rows as they were (a real compression needs a long chat and a real model).
        assert compressed["status"] == "noop" and compressed["summary"].get("noop") is True, compressed

    listener.run(script)


def test_dashboard_shapes_are_refused_before_anything_runs(listener):
    """Conduit resends a request built before the ready in the authority's shape;
    that is only safe while the authority refuses the dashboard's shape outright."""

    async def script(sock: ConduitSocket) -> None:
        dashboard_create = await sock.call("session.create", {"cols": 96, "source": "desktop"})
        assert _refusal(dashboard_create) == "invalid_params", dashboard_create

        sid = (await sock.result("session.create", _create()))["session_id"]
        no_id = await sock.call("prompt.submit", {"session_id": sid, "text": "hello"})
        assert _refusal(no_id) == "invalid_params", no_id
        with_images = await sock.call("session.resume", {**_resume(sid, omit_messages=False), "inline_images": False})
        assert _refusal(with_images) == "invalid_params", with_images
        for method in ("session.title", "session.compress"):
            missing = await sock.call(method, {"session_id": sid})
            assert missing.get("error", {}).get("code") == -32601, (method, missing)

        # A turn the refused prompt had started would show within the fake model's first words.
        await asyncio.sleep(3)
        snapshot = await sock.result("session.resume", _resume(sid, omit_messages=True))
        assert snapshot["message_count"] == 0 and snapshot["running"] is False, snapshot
        assert not [e for e in sock.events if e.get("type") == "message.start" and e.get("session_id") == sid]

    listener.run(script)


def test_prompts_are_answered_from_a_socket_that_never_saw_them_asked(listener):
    """A card shown before a reconnect: Conduit reads its prompt id and turn
    from the chat's resume (HermesClient.respondToApproval and
    respondToClarification) and answers in the authority's shape."""

    for text, method, answer in (("RUN it", "approval.respond", {"choice": "deny"}),
                                 ("ASK me", "clarify.respond", {"answer": "a"})):
        sid = _ask(listener, text)
        async def answer_it(sock: ConduitSocket) -> None:
            snapshot = await sock.result("session.resume", _resume(sid, omit_messages=True))
            prompts = snapshot["prompts"]
            assert len(prompts) == 1 and prompts[0].get("prompt_id"), snapshot
            assert isinstance(prompts[0].get("execution_generation"), int), prompts
            if method == "approval.respond":
                # Conduit fills a card's id from approval.pending: the same id.
                pending = await sock.result("approval.pending", {"session_id": sid})
                ids = [a.get("id") or a.get("request_id") or a.get("prompt_id") for a in pending.get("approvals") or []]
                assert prompts[0]["prompt_id"] in ids, pending
            done = await sock.result(method, {"session_id": sid, "prompt_id": prompts[0]["prompt_id"],
                                              "execution_generation": prompts[0]["execution_generation"], **answer})
            assert done.get("status") == "resolved", done
            gone = await sock.result("session.resume", _resume(sid, omit_messages=True))
            assert gone["prompts"] == [], gone
        listener.run(answer_it)


def _ask(listener: "Listener", text: str) -> str:
    """Starts a turn that waits on a prompt, on a socket that then closes."""
    async def script(sock: ConduitSocket) -> str:
        sid = (await sock.result("session.create", _create()))["session_id"]
        await sock.result("prompt.submit", _submit(sid, text))
        await sock.event(sid, "clarify.request" if "ASK" in text else "approval.request")
        return sid
    return listener.run(script)


def test_a_guarded_model_pick_asks_then_applies_with_its_token(listener):
    async def script(sock: ConduitSocket) -> None:
        sid = (await sock.result("session.create", _create()))["session_id"]
        await sock.result("prompt.submit", _submit(sid, "hello"))
        await sock.event(sid, "message.complete")
        snapshot = await sock.result("session.resume", _resume(sid, omit_messages=True))
        pick = {"model": "fake-model-2", "provider": "custom"}
        asked = await sock.result("session.mutate", _mutate(snapshot, sid, "model", pick, fences_generation=True))
        assert asked.get("status") == "confirmation_required" and asked.get("confirm_required") is True, asked
        assert asked.get("confirm") and asked.get("confirm_message") and asked.get("target_model"), asked
        assert "model" not in asked, "nothing applied yet"
        applied = await sock.result("session.mutate", _mutate(snapshot, sid, "model",
                                                               {**pick, "confirm": asked["confirm"]},
                                                               fences_generation=True))
        assert applied.get("model") == asked["target_model"], applied

    listener.run(script)


# --- The plugin's features on the authority's chats ---------------------------
#
# The plugin's routes and its own chats used to find a chat in the dashboard's
# TUI gateway, which a gateway's listener doesn't run chats in. These check
# each feature finds the authority's chat, or drives its own there
# (gateway_chats.py), and that what the turn hears is right.

def _told(text: str) -> List[str]:
    """What the turns whose last user message is ``text`` were told around it."""
    told = []
    for request in list(_FakeModel.seen):
        users = [m for m in request["messages"] if m.get("role") == "user"]
        content = users[-1].get("content") if users else None
        words = content if isinstance(content, str) else json.dumps(content)
        if text in words:
            told.append(words)
    return told


def _turn(listener: "Listener", text: str) -> str:
    """A chat with one finished turn of ``text``, left open on the authority."""
    async def script(sock: ConduitSocket) -> str:
        sid = (await sock.result("session.create", _create()))["session_id"]
        await sock.result("prompt.submit", _submit(sid, text))
        await sock.event(sid, "message.complete")
        return sid
    return listener.run(script)


def test_a_turn_hears_where_in_conduit_its_message_was_sent(listener):
    text = "where should I stop for lunch"

    async def script(sock: ConduitSocket) -> None:
        sid = (await sock.result("session.create", _create()))["session_id"]
        # What Conduit posts just before it sends the message (TurnSurfaceReporter).
        status, body = listener.plugin_call("turn-surface", {
            "session_id": sid, "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "surface": "carplay", "input": "spoken"})
        assert status == 200 and body.get("recorded") is True, body
        await sock.result("prompt.submit", _submit(sid, text))
        await sock.event(sid, "message.complete")

    listener.run(script)
    told = _told(text)
    assert told and "[Conduit: this message came from CarPlay in the user's car, spoken." in told[0], told


def test_predictions_answer_for_a_chat_the_authority_runs(listener):
    sid = _turn(listener, "plan my week")
    status, body = listener.plugin_call("predict", {"session_id": sid, "kind": "replies", "count": 2, "wait_s": 30},
                                        timeout=60)
    assert status == 200, body
    assert body.get("parsed") is True and [r.get("text") for r in body.get("replies")] == ["sounds good"], body


def test_watch_jobs_session_api_runs_its_chats_on_the_authority(listener):
    status, out = listener.plugin_call("session-api-turn", plugin=DRIVER, timeout=180)
    assert status == 200, out
    assert out["api"] == "_AuthorityChats", out
    assert "error" not in out["create"] and "error" not in out["submit"], out
    # The authority's prompt id under the TUI gateway's name, and its answer's count.
    assert out["approval"]["request_id"] == out["approval"]["prompt_id"], out
    assert out["viewer"] is True, out
    assert out["respond"].get("result") == {"resolved": 1}, out
    assert out["settled"] == {"request_ids": [out["approval"]["prompt_id"]]}, out
    assert out["complete"], out
    # After the turn: a steer finds nothing running, a requestless answer nothing waiting.
    assert out["steer_after"].get("result") == {"status": "rejected"}, out
    assert out["again"].get("result") == {"resolved": 0}, out
    assert "error" not in out["close"] and out["viewer_after_close"] is False, out
    # The job's turn names the Watch, as its transport says.
    told = _told("RUN the job")
    assert told and "[Conduit: this message came from the user's Apple Watch, spoken." in told[0], told

    # The chat is the authority's: the phone opens it, with the job's turn in it.
    async def script(sock: ConduitSocket) -> List[Any]:
        full = await sock.result("session.resume", _resume(out["session_id"], omit_messages=False))
        return [m.get("text") or m.get("content") for m in full["messages"] if m.get("role") == "user"]
    assert "RUN the job" in listener.run(script)


def test_a_watch_calls_local_live_chat_runs_on_the_authority(listener):
    status, out = listener.plugin_call("watch-call-turn", plugin=DRIVER, timeout=180)
    assert status == 200, out
    # The authority admits every send to its FIFO.
    assert out["status"] in ("queued", "started"), out
    assert out["finished"] is True and "message.complete" in out["kinds"], out
    told = _told("hello from the watch")
    assert told and "[Conduit: this message came from the user's Apple Watch, spoken." in told[0], told


def test_pulse_sends_its_turn_into_its_chat_on_the_authority(listener):
    sid = _turn(listener, "my pulse chat")
    status, out = listener.plugin_call(f"pulse-send?chat_id={sid}", plugin=DRIVER, timeout=180)
    assert status == 200, out
    # An idle chat's turn may start before the receipt comes back ("started", read as streaming).
    assert out["available"] is True and out["ok"] is True, out
    assert (out["status"], out["detail"]) in {("queued", "queued"), ("streaming", "started")}, out
    deadline = time.monotonic() + 60
    while not _told("PULSE check in"):
        assert time.monotonic() < deadline, "Pulse's turn never ran"
        time.sleep(0.5)
    # On Pulse's model: the guarded switch was confirmed with its token.
    assert any(r["model"] == "fake-model-2" for r in _FakeModel.seen
               if "PULSE check in" in json.dumps(r["messages"])), [r["model"] for r in _FakeModel.seen[-3:]]



def test_bot_chat_create_and_redirect_in_conduits_shapes(listener):
    async def script(sock: ConduitSocket) -> None:
        # HermesClient.createBotChatSession on the authority: born hidden, following the profile.
        bot = await sock.result("session.create", {**_create(), "title": "Bot Chat", "hidden": True,
                                                   "follow_profile_config": True})
        assert bot["session_id"] and bot["stored_session_id"], bot

        # A redirect answers one of the words SessionRedirectOutcome reads.
        sid = (await sock.result("session.create", _create()))["session_id"]
        await sock.result("prompt.submit", _submit(sid, "first"))
        start = await sock.event(sid, "message.start")
        redirected = await sock.result("session.redirect", {
            "session_id": sid, "execution_generation": start["execution_generation"], "text": "do this instead"})
        assert redirected["status"] in {"redirected", "queued", "rejected"}, redirected
        await sock.event(sid, "message.complete")

    listener.run(script)


# A 1x1 PNG.
_PIXEL = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGA"
          "WjR9awAAAABJRU5ErkJggg==")


def test_a_staged_picture_reaches_the_turn_its_prompt_names(listener):
    """The authority only stages an image; Conduit names it in the prompt's attachments."""
    text = "what is in this picture"

    async def script(sock: ConduitSocket) -> None:
        sid = (await sock.result("session.create", _create()))["session_id"]
        staged = await sock.result("image.attach_bytes", {"session_id": sid, "content_base64": _PIXEL,
                                                          "filename": "pixel.png"})
        assert staged.get("path") and staged.get("mime") == "image/png", staged
        await sock.result("prompt.submit", {**_submit(sid, text),
                                            "attachments": [{"path": staged["path"], "mime": staged["mime"]}]})
        await sock.event(sid, "message.complete")

    before = len(_FakeModel.seen)
    listener.run(script)
    # The picture reaches a model: the turn's own, or (for a model that can't see,
    # like this one) the vision step that describes it for the turn.
    seen = json.dumps(_FakeModel.seen[before:])
    assert _PIXEL[:40] in seen, "the staged picture never reached a model"

