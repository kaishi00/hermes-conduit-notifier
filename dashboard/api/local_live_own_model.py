"""Local Live on a speech server whose own model answers."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
import contextlib
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

from . import core, local_live, local_live_agent, local_live_read_back

logger = core.logger


# --- Local Live on a speech server whose own model answers ---------------------
#
# A hosted realtime model (OpenAI Realtime, Qwen's) can hold the call itself,
# the way Grok Live's does: it listens, answers and speaks, and hands the
# work to Hermes through the call's tools. With voice.speech_server.own_model
# a Live > Local call in agent mode (the phone sent conduit.agent) runs that
# way, on the call's relay:
#
#   phone: session.update + conduit.agent {instructions, tools}
#     -> server: one session.update with the call's instructions and tools,
#        the set voice and transcription model; its turn detection stays on
#   server: a function call -> phone: conduit.tool.call
#   phone: conduit.tool.result -> server: function_call_output, response.create
#   phone: conduit.agent.note -> server: a user item it keeps, unanswered
#
# The server's audio, transcripts and responses pass both ways unchanged, so
# the phone plays them as it plays any Local Live reply. The readiness check
# is still the phone's own turn: its greeting, when there is one, is read
# aloud by the server as written.
#
# A call with no agent setup (Classic's hands-free option, the Watch) keeps
# Hermes turns: the plugin runs them, as with voice.speech_server.drive_turns
# (local_live_read_back). No fast model is asked on either.

# How long the phone has to answer the readiness check with its greeting.
LOCAL_LIVE_OWN_MODEL_READY_S = 10.0
# A response asked for after a tool's answer that the server says nothing
# about in this long is asked for again with the next answer.
LOCAL_LIVE_OWN_MODEL_ASK_S = 5.0
LOCAL_LIVE_OWN_MODEL_MAX_CALLS = 256
_LOCAL_LIVE_OWN_MODEL_AUDIO = ('"response.output_audio.delta"', '"response.audio.delta"')
_LOCAL_LIVE_OWN_MODEL_KINDS = ('"response.created"', '"response.done"', "function_call", '"error"')


def _local_live_realtime_tool(tool: Dict[str, Any]) -> Dict[str, Any]:
    """An agent's tool (chat completions' shape) as a Realtime session takes it."""
    function = tool.get("function") if isinstance(tool.get("function"), dict) else {}
    return {"type": "function", **function}


class _LocalLiveOwnModel:
    """One Local Live connection on a server whose own model answers (see
    above). It stands where the relay's read-back does and holds one: until
    the call brings an agent setup, and for a call that never does, every
    frame goes through the read-back, which runs Hermes turns.

    Everything runs on the relay's loop.
    """

    def __init__(self, call: Any, to_client: Callable[[str], Awaitable[None]], read_back: Any, setup: Any,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.call = call
        self.to_client = to_client
        self.read_back = read_back
        self.setup = setup
        self.clock = clock
        self.to_server: Optional[Callable[[str], Awaitable[None]]] = None
        # The server's model has the call: the read-back sees nothing more.
        self.mine = False
        # The call's session.update, as it came.
        self.update: Optional[Dict[str, Any]] = None
        # A response is running on the server.
        self.active = False
        # A tool's answer waits to be spoken: a response is asked for once
        # every tool has answered and none is running.
        self.owed = False
        self.pending = 0
        # When a response was last asked for, until the server starts it.
        self.asked_at: Optional[float] = None
        self.seen: Set[str] = set()
        self.tasks: Set["asyncio.Task[None]"] = set()
        self.closed = False
        call.own_model = self

    def attach(self, to_server: Callable[[str], Awaitable[None]],
               close_server: Optional[Callable[[], Awaitable[None]]] = None) -> None:
        self.to_server = to_server
        self.read_back.attach(to_server, close_server)

    def close(self) -> None:
        self.closed = True
        for task in list(self.tasks):
            task.cancel()
        self.read_back.close()

    # The call's frames.

    def passed_on(self, text: str) -> str:
        return text if self.mine else self.read_back.passed_on(text)

    async def from_client(self, text: str) -> bool:
        """True for a frame that stays here."""
        if self.closed:
            return False
        update = self._frame(text, "session.update") if '"session.update"' in text else None
        if update is not None:
            self.update = update
            if self.mine or getattr(self.call, "agent", None) is not None:
                await self._take()
                return True
        if not self.mine:
            return await self.read_back.from_client(text)
        if '"response.create"' in text:
            frame = self._frame(text, "response.create")
            response = frame.get("response") if frame is not None and isinstance(frame.get("response"), dict) else {}
            inline = response.get("input")
            if isinstance(inline, list):
                words = " ".join(word for word in (local_live_read_back._local_live_item_words(item)
                                                   for item in inline) if word)
                if words.startswith(local_live_agent.LOCAL_LIVE_READY_PREFIX):
                    self._spawn(self._ready(words[:local_live.LOCAL_LIVE_MAX_TURN_CHARS]))
                    return True
        return False

    async def agent_changed(self) -> None:
        """The call set its agent up (conduit.agent). One that came after
        the session.update takes the call over from the read-back."""
        if not self.closed and self.update is not None:
            await self._take()

    async def note(self, text: Any) -> None:
        """Context the model keeps without answering it."""
        if not self.mine or self.closed or not isinstance(text, str) or not text.strip():
            return
        words = text.strip()[:local_live_agent.LOCAL_LIVE_AGENT_MAX_NOTE_CHARS]
        await self._send({"type": "conversation.item.create",
                          "item": {"type": "message", "role": "user",
                                   "content": [{"type": "input_text", "text": words}]}})

    # The server's frames.

    async def from_server(self, text: str) -> Optional[str]:
        """The frame to pass on to the call, or None for one that stays here."""
        if not self.mine:
            return await self.read_back.from_server(text)
        if self.closed or any(kind in text for kind in _LOCAL_LIVE_OWN_MODEL_AUDIO) \
                or not any(kind in text for kind in _LOCAL_LIVE_OWN_MODEL_KINDS):
            return text
        try:
            event = json.loads(text)
        except ValueError:
            return text
        kind = event.get("type") if isinstance(event, dict) else None
        if kind == "response.created":
            self.active = True
            self.asked_at = None
            return text
        if kind == "response.done":
            self.active = False
            self.asked_at = None
            response = event.get("response") if isinstance(event.get("response"), dict) else {}
            output = response.get("output") if isinstance(response.get("output"), list) else []
            for item in output:
                self._call(item)
            await self._settle()
            return text
        if kind == "error":
            # A refused response.create: the next answer asks again.
            self.asked_at = None
            return text
        if kind == "response.function_call_arguments.delta":
            return None
        if kind == "response.function_call_arguments.done":
            self._call({**event, "type": "function_call"})
            return None
        if kind in ("response.output_item.added", "response.output_item.done"):
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "function_call":
                if kind == "response.output_item.done":
                    self._call(item)
                return None
        return text

    # The call's setup.

    async def _take(self) -> None:
        agent = getattr(self.call, "agent", None)
        if agent is None or self.update is None:
            return
        if not self.mine:
            self.mine = True
            logger.info("Local Live: the speech server's own model answers this call")
            profile = getattr(self.call, "profile", None)
            if isinstance(profile, str):
                # It answers by design here: Settings has nothing to warn of.
                local_live._local_live_answered_alone.pop(profile, None)
        frame = json.loads(json.dumps(self.update))
        tools = [_local_live_realtime_tool(tool) for tool in agent.tools]
        await self._send(self.setup.own_update(frame, agent.instructions, tools))

    async def _ready(self, words: str) -> None:
        """The readiness check: the phone's own turn, as on any Local Live
        call. Its greeting is read aloud as written; with none, the call
        still sees a response begin and end."""
        turn = await self.call.start_turn(words)
        said: List[str] = []
        if turn is not None:
            deadline = self.clock() + LOCAL_LIVE_OWN_MODEL_READY_S
            try:
                while True:
                    kind, delta = await asyncio.wait_for(turn.frames.get(), max(0.05, deadline - self.clock()))
                    if kind == "done":
                        break
                    said.append(delta)
            except asyncio.TimeoutError:
                await self.call.cancel_turn(turn)
        if self.closed:
            return
        greeting = " ".join("".join(said).split())[:local_live.LOCAL_LIVE_MAX_REPLY_CHARS]
        if greeting:
            item = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": greeting}]}
            await self._send(self.setup.say({"type": "response.create",
                                             "response": {"conversation": "none", "input": [item]}}))
            return
        response_id = f"resp_conduit_{uuid.uuid4().hex[:16]}"
        for frame in (local_live_read_back._local_live_read_back_response(None, response_id),
                      local_live_read_back._local_live_read_back_response("completed", response_id)):
            with contextlib.suppress(Exception):
                await self.to_client(frame)

    # Tools.

    def _call(self, item: Any) -> None:
        """A function call the model made, run once on the phone."""
        if not isinstance(item, dict) or item.get("type") != "function_call":
            return
        call_id, name = item.get("call_id"), item.get("name")
        if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name \
                or call_id in self.seen or len(self.seen) >= LOCAL_LIVE_OWN_MODEL_MAX_CALLS:
            return
        self.seen.add(call_id)
        arguments = item.get("arguments")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments if isinstance(arguments, dict) else {})
        self.pending += 1
        self._spawn(self._run(call_id, name, arguments))

    async def _run(self, call_id: str, name: str, arguments: str) -> None:
        try:
            agent = getattr(self.call, "agent", None)
            if agent is None:
                output = json.dumps({"error": "The call has no tools."})
            else:
                output = await agent.run_tool(self.call, name, arguments, call_id=call_id)
            if self.closed:
                return
            await self._send({"type": "conversation.item.create",
                              "item": {"type": "function_call_output", "call_id": call_id, "output": output}})
            self.owed = True
        finally:
            self.pending -= 1
        await self._settle()

    async def _settle(self) -> None:
        """Asks for the response that speaks the tools' answers, once all of
        them are in and the server is free."""
        if self.closed or not self.owed or self.pending > 0 or self.active:
            return
        if self.asked_at is not None and self.clock() - self.asked_at < LOCAL_LIVE_OWN_MODEL_ASK_S:
            return
        self.owed = False
        self.asked_at = self.clock()
        await self._send({"type": "response.create"})

    # Plumbing.

    @staticmethod
    def _frame(text: str, kind: str) -> Optional[Dict[str, Any]]:
        try:
            frame = json.loads(text)
        except ValueError:
            return None
        return frame if isinstance(frame, dict) and frame.get("type") == kind else None

    def _spawn(self, coroutine: Any) -> None:
        task = asyncio.ensure_future(coroutine)
        self.tasks.add(task)
        local_live._local_live_tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        task.add_done_callback(local_live._local_live_task_done)

    async def _send(self, frame: Dict[str, Any]) -> None:
        if self.to_server is not None and not self.closed:
            with contextlib.suppress(Exception):
                await self.to_server(json.dumps(frame))
