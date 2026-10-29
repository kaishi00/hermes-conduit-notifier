"""Local Live agent mode: a fast front model in front of Hermes."""

from __future__ import annotations

import asyncio
import itertools
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
import contextlib
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException, Request, Response

from . import core, gemini_qwen_live, local_live, speech_server

logger = core.logger
router = core.router


# --- Local Live agent mode: a fast front model in front of Hermes ------------
#
# One Hermes turn per utterance is slow to start talking (a full agent turn,
# often 8-15 s). In agent mode a quick model of the user's choosing holds the
# conversation and hands real work to Hermes as background jobs, like
# GPT-Live, Gemini and Grok do:
#
#   phone -> plugin: conduit.agent {instructions, tools}   (on connecting)
#   speech server -> /local-live/v1/chat/completions       (each utterance)
#   plugin -> front model (voice.local_live.front_model), streamed back
#   front model calls a tool -> plugin -> phone: conduit.tool.call
#   phone runs it (start_job, list_jobs, cancel_job, ...) -> conduit.tool.result
#
# The phone owns the tools and the jobs, exactly as for Grok Live: it starts
# a job in the call's chat and answers at once, and a job's outcome comes
# back later as a typed turn the phone sends while the call is quiet, which
# the speech server asks this route about like any utterance. The speech
# server never sees a tool (HF speech-to-speech errors on tool_choice).
#
# With no front model configured the call goes on as before: one Hermes turn
# per utterance, through conduit.turn. An engine that runs its own front
# model (Pipecat behind the relay, say) uses the same tools through
# /local-live/v1/tools and /local-live/v1/tools/call with the Local Live key.

LOCAL_LIVE_READY_PREFIX = "conduit-ready-"
LOCAL_LIVE_AGENT_MAX_INSTRUCTIONS = 64_000
LOCAL_LIVE_AGENT_MAX_TOOLS = 64
LOCAL_LIVE_AGENT_MAX_NOTE_CHARS = 4_000
# Messages kept for the front model: the call's recent conversation, tool
# calls included. Older ones go first, a user message always leads.
LOCAL_LIVE_AGENT_MAX_HISTORY = 60
LOCAL_LIVE_AGENT_MAX_HISTORY_CHARS = 60_000
LOCAL_LIVE_AGENT_MAX_TOOL_OUTPUT_CHARS = 16_000
# Tool rounds in one reply: a model that keeps calling tools stops here.
LOCAL_LIVE_AGENT_MAX_ROUNDS = 5
# How long one tool call waits for the phone. start_job answers once the
# job runs; a web lookup takes a few seconds.
LOCAL_LIVE_AGENT_TOOL_TIMEOUT_S = 30.0
# To connect, and between chunks of the front model's stream.
LOCAL_LIVE_FRONT_TIMEOUT_S = 30.0
LOCAL_LIVE_FRONT_MAX_LINE_BYTES = 1024 * 1024
LOCAL_LIVE_FRONT_UNREACHABLE_REPLY = "I can't reach my quick model right now, so I'll pass that to Hermes."
LOCAL_LIVE_AGENT_SPEECH_HINT = (
    "\nEverything you write is spoken aloud by a speech server as it streams: plain spoken sentences only, "
    "no markdown, lists, code, emoji or links. Keep replies short unless the user asks for more.")
_LOCAL_LIVE_TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
_local_live_front_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="conduit-local-live-front")
_local_live_front_opener = urllib.request.build_opener(gemini_qwen_live._NoRedirect)
# Plain http only reaches private hosts; like the pairing claim, it skips any
# proxy so the key never crosses one in cleartext.
_local_live_front_direct_opener = urllib.request.build_opener(gemini_qwen_live._NoRedirect, urllib.request.ProxyHandler({}))


def _local_live_front_model_settings() -> Dict[str, Any]:
    """The profile's ``voice.local_live.front_model`` block (profile-scoped), or {}."""
    try:
        from hermes_cli.config import load_config
        config = load_config()
    except ImportError:
        return {}
    except Exception as exc:
        logger.warning("Local Live could not read the Hermes config: %s", type(exc).__name__)
        raise core.TokenError(503, f"The Hermes host could not read its config ({type(exc).__name__}).")
    voice = config.get("voice") if isinstance(config, dict) else None
    local = voice.get("local_live") if isinstance(voice, dict) else None
    front = local.get("front_model") if isinstance(local, dict) else None
    return front if isinstance(front, dict) else {}


def local_live_front_model_url(raw: Any) -> str:
    """The configured base URL as its chat-completions address.

    Any OpenAI-compatible server: ``http://box:8080/v1`` asks
    ``http://box:8080/v1/chat/completions``. Plain http only reaches private
    hosts, since the key travels with every request.
    """
    text = str(raw or "").strip()
    if not text:
        raise core.TokenError(503, "No fast model is set for Local Live (voice.local_live.front_model.base_url).")
    if "://" not in text:
        text = "http://" + text
    try:
        parts = urllib.parse.urlsplit(text)
        port = parts.port
    except ValueError:
        raise core.TokenError(503, "The fast model's address is not valid.")
    scheme = parts.scheme.lower()
    host = parts.hostname or ""
    if scheme not in ("http", "https") or not host or parts.username or parts.password or parts.fragment \
            or any(ord(c) <= 0x20 or ord(c) == 0x7F for c in text):
        raise core.TokenError(503, "The fast model's address must look like http://host:8080/v1.")
    if scheme == "http" and not speech_server._speech_server_host_is_private(host):
        raise core.TokenError(503, f"{host} is a public address, so the fast model needs https://.")
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    path = parts.path.rstrip("/")
    if not path.endswith("/chat/completions"):
        path += "/chat/completions"
    return urllib.parse.urlunsplit((scheme, netloc, path, parts.query, ""))


def local_live_front_model_target(settings: Optional[Dict[str, Any]] = None) -> Tuple[str, str, str]:
    """(url, model, key) for this profile's front model; TokenError(503) when unset or invalid."""
    if settings is None:
        settings = _local_live_front_model_settings()
    url = local_live_front_model_url(settings.get("base_url"))
    model = str(settings.get("model") or "").strip()
    if not model:
        raise core.TokenError(503, "Local Live's fast model has no model name (voice.local_live.front_model.model).")
    try:
        key = speech_server._speech_server_key(settings)
    except core.TokenError:
        raise core.TokenError(503, "The fast model's key file (voice.local_live.front_model.api_key_file) can't be used.")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in key + model):
        raise core.TokenError(503, "The fast model's key or name contains a control character.")
    return url, model, key


def local_live_front_model_status() -> Dict[str, Any]:
    """What Conduit's Local settings show about the front model."""
    try:
        settings = _local_live_front_model_settings()
    except core.TokenError as exc:
        return {"configured": False, "available": False, "base_url": None, "model": None, "has_key": False,
                "reason": str(exc)}
    base_url = str(settings.get("base_url") or "").strip() or None
    model = str(settings.get("model") or "").strip() or None
    has_key = bool(str(settings.get("api_key") or "").strip() or str(settings.get("api_key_file") or "").strip())
    configured = base_url is not None or model is not None
    try:
        local_live_front_model_target(settings)
    except core.TokenError as exc:
        return {"configured": configured, "available": False, "base_url": base_url, "model": model,
                "has_key": has_key, "reason": str(exc) if configured else None}
    return {"configured": True, "available": True, "base_url": base_url, "model": model, "has_key": has_key,
            "reason": None}


def _local_live_agent_tool(raw: Any) -> Optional[Dict[str, Any]]:
    """A tool as chat completions takes it, from either the Realtime shape
    ({type, name, description, parameters}) or the chat one ({type, function})."""
    if not isinstance(raw, dict):
        return None
    function = raw.get("function") if isinstance(raw.get("function"), dict) else raw
    name = function.get("name")
    if not isinstance(name, str) or not _LOCAL_LIVE_TOOL_NAME.fullmatch(name):
        return None
    parameters = function.get("parameters")
    tool: Dict[str, Any] = {"name": name,
                            "parameters": parameters if isinstance(parameters, dict) else {"type": "object",
                                                                                           "properties": {}}}
    if isinstance(function.get("description"), str):
        tool["description"] = function["description"]
    return {"type": "function", "function": tool}


class _LocalLiveAgent:
    """One call's front-model conversation and the tool calls it waits on."""

    def __init__(self, instructions: str, tools: List[Dict[str, Any]]) -> None:
        self.instructions = instructions
        self.tools = tools
        self.history: List[Dict[str, Any]] = []
        self.waiters: Dict[str, "asyncio.Future[str]"] = {}
        # Bumped by each new reply: an older one stops at its next step.
        self.generation = 0
        self.numbers = itertools.count(1)

    @classmethod
    def configured(cls, frame: Dict[str, Any], previous: Optional["_LocalLiveAgent"] = None) -> "_LocalLiveAgent":
        instructions = frame.get("instructions")
        instructions = instructions[:LOCAL_LIVE_AGENT_MAX_INSTRUCTIONS] if isinstance(instructions, str) else ""
        raw_tools = frame.get("tools") if isinstance(frame.get("tools"), list) else []
        tools, names = [], set()
        for raw in raw_tools[:LOCAL_LIVE_AGENT_MAX_TOOLS]:
            tool = _local_live_agent_tool(raw)
            if tool is not None and tool["function"]["name"] not in names:
                names.add(tool["function"]["name"])
                tools.append(tool)
        agent = cls(instructions, tools)
        if previous is not None:
            agent.history = previous.history
            agent.numbers = previous.numbers
            previous.close()
        return agent

    def note(self, text: Any) -> None:
        """Context the front model keeps without answering it (a turn typed
        in the attached chat)."""
        if isinstance(text, str) and text.strip():
            self._append({"role": "user", "content": text.strip()[:LOCAL_LIVE_AGENT_MAX_NOTE_CHARS]})

    def messages(self) -> List[Dict[str, Any]]:
        system = (self.instructions or "You are a helpful voice assistant.") + LOCAL_LIVE_AGENT_SPEECH_HINT
        messages: List[Dict[str, Any]] = [{"role": "system", "content": system}]
        # Every tool call needs its result next, or the server refuses the
        # conversation: one the user talked over gets a stand-in.
        owed: List[str] = []
        for message in self.history:
            if message.get("role") == "tool":
                if message.get("tool_call_id") in owed:
                    owed.remove(message["tool_call_id"])
                    messages.append(message)
                continue
            messages.extend(self._stand_ins(owed))
            owed = [item["id"] for item in message.get("tool_calls") or []]
            messages.append(message)
        messages.extend(self._stand_ins(owed))
        return messages

    @staticmethod
    def _stand_ins(owed: List[str]) -> List[Dict[str, Any]]:
        return [{"role": "tool", "tool_call_id": call_id,
                 "content": json.dumps({"status": "interrupted", "note": "The user spoke before this answered."})}
                for call_id in owed]

    def _append(self, message: Dict[str, Any]) -> None:
        self.history.append(message)
        total = sum(len(json.dumps(m)) for m in self.history)
        while self.history and (len(self.history) > LOCAL_LIVE_AGENT_MAX_HISTORY
                                or total > LOCAL_LIVE_AGENT_MAX_HISTORY_CHARS):
            total -= len(json.dumps(self.history.pop(0)))
            # Never start on a tool result or an assistant's tool calls whose
            # user message went: the server would refuse the conversation.
            while self.history and self.history[0].get("role") != "user":
                total -= len(json.dumps(self.history.pop(0)))

    def resolve(self, call_id: str, output: str) -> bool:
        waiter = self.waiters.pop(call_id, None)
        if waiter is None or waiter.done():
            return False
        waiter.set_result(output[:LOCAL_LIVE_AGENT_MAX_TOOL_OUTPUT_CHARS])
        return True

    def close(self) -> None:
        self.generation += 1
        for waiter in self.waiters.values():
            if not waiter.done():
                waiter.cancel()
        self.waiters.clear()

    async def run_tool(self, call: Any, name: str, arguments: str, call_id: Optional[str] = None) -> str:
        """Asks the phone to run one tool; its output, or an error the model reads."""
        call_id = call_id or f"ll{next(self.numbers)}"
        waiter: "asyncio.Future[str]" = asyncio.get_running_loop().create_future()
        self.waiters[call_id] = waiter
        sent = await call.send({"type": "conduit.tool.call", "call_id": call_id, "name": name,
                                "arguments": arguments or "{}"})
        if not sent:
            self.waiters.pop(call_id, None)
            return json.dumps({"error": "Conduit isn't connected, so the tool couldn't run."})
        try:
            return await asyncio.wait_for(asyncio.shield(waiter), timeout=LOCAL_LIVE_AGENT_TOOL_TIMEOUT_S)
        except asyncio.TimeoutError:
            # Kept waiting: a late answer still reaches the phone as an update.
            self.waiters.pop(call_id, None)
            return json.dumps({"status": "still_running",
                               "note": "No answer yet. If it comes, it arrives later as its own message."})
        except asyncio.CancelledError:
            if waiter.cancelled():
                return json.dumps({"error": "The call ended."})
            # The reply was dropped (the user talked over it): a later
            # answer is the phone's to say.
            self.waiters.pop(call_id, None)
            raise

    def _drop_unanswered(self, text: str) -> None:
        if self.history and self.history[-1] == {"role": "user", "content": text}:
            self.history.pop()

    async def reply(self, call: Any, text: str, target: Tuple[str, str, str]) -> Any:
        """The front model's spoken answer to ``text``, as it streams. Raises
        _LocalLiveFrontUnavailable when the model failed before saying anything."""
        self.generation += 1
        generation = self.generation
        self._append({"role": "user", "content": text})
        said_anything = False
        budget = local_live.LOCAL_LIVE_MAX_REPLY_CHARS
        for round_number in range(LOCAL_LIVE_AGENT_MAX_ROUNDS):
            spoken: List[str] = []
            tool_calls: Dict[int, Dict[str, Any]] = {}
            completed = False
            try:
                async with contextlib.aclosing(_local_live_front_stream(target, self.messages(), self.tools)) as stream:
                    async for kind, value in stream:
                        if generation != self.generation:
                            # A reconnect took over: the new connection
                            # starts with a conversation that alternates.
                            if not said_anything and not tool_calls:
                                self._drop_unanswered(text)
                            return
                        if kind == "text":
                            # A model that never stops talking is cut off
                            # like an over-long Hermes reply.
                            value = value[:budget]
                            budget -= len(value)
                            spoken.append(value)
                            said_anything = True
                            yield value
                            if budget <= 0:
                                return
                        elif kind == "tool_call":
                            slot = tool_calls.setdefault(value["index"], {"id": "", "name": "", "arguments": ""})
                            slot["id"] = value.get("id") or slot["id"]
                            slot["name"] = value.get("name") or slot["name"]
                            slot["arguments"] += value.get("arguments") or ""
                completed = True
                for index, slot in tool_calls.items():
                    slot["id"] = slot["id"] or f"call_{generation}_{index}"
            except _LocalLiveFrontUnavailable:
                if said_anything:
                    return
                self._drop_unanswered(text)
                raise
            finally:
                # What was said stays in the conversation, even when the user
                # talked over it and the stream was dropped.
                if spoken or (completed and tool_calls):
                    message: Dict[str, Any] = {"role": "assistant", "content": "".join(spoken)}
                    if completed and tool_calls:
                        message["tool_calls"] = [
                            {"id": slot["id"], "type": "function",
                             "function": {"name": slot["name"], "arguments": slot["arguments"] or "{}"}}
                            for _, slot in sorted(tool_calls.items()) if slot["name"]]
                        if not message["tool_calls"]:
                            del message["tool_calls"]
                    self._append(message)
            calls = [slot for _, slot in sorted(tool_calls.items()) if slot["name"]]
            if not calls:
                if round_number == 0 and not said_anything:
                    # An empty answer (a wrong address that answers with a
                    # web page, say) is a failure too: Hermes takes it.
                    self._drop_unanswered(text)
                    raise _LocalLiveFrontUnavailable("The fast model gave no answer")
                return
            for slot in calls:
                if generation != self.generation:
                    return
                output = await self.run_tool(call, slot["name"], slot["arguments"])
                self._append({"role": "tool", "tool_call_id": slot["id"], "content": output})
            if generation != self.generation:
                return


class _LocalLiveFrontUnavailable(Exception):
    """The front model couldn't be asked (unreachable, refused, broken stream)."""


class _ThinkFilter:
    """Drops <think>…</think> from a streamed reply: reasoning models put
    their thinking there, and the speech server would read it out."""

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self) -> None:
        self.thinking = False
        self.pending = ""

    def feed(self, text: str) -> str:
        self.pending += text
        out: List[str] = []
        while self.pending:
            tag = self.CLOSE if self.thinking else self.OPEN
            at = self.pending.find(tag)
            if at >= 0:
                if not self.thinking:
                    out.append(self.pending[:at])
                self.pending = self.pending[at + len(tag):]
                self.thinking = not self.thinking
                continue
            # Keep a tail that could be the start of the tag.
            keep = next((n for n in range(len(tag) - 1, 0, -1) if self.pending.endswith(tag[:n])), 0)
            if not self.thinking:
                out.append(self.pending[:len(self.pending) - keep])
            self.pending = self.pending[len(self.pending) - keep:]
            break
        return "".join(out)

    def flush(self) -> str:
        rest, self.pending = ("" if self.thinking else self.pending), ""
        return rest


async def _local_live_front_stream(target: Tuple[str, str, str], messages: List[Dict[str, Any]],
                                   tools: List[Dict[str, Any]]) -> Any:
    """("text", words) and ("tool_call", {index, id, name, arguments}) from
    the front model as they arrive. The request runs on a worker thread
    (urllib), which stops at its next line once this generator closes."""
    url, model, key = target
    body: Dict[str, Any] = {"model": model, "messages": messages, "stream": True}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    loop = asyncio.get_running_loop()
    queue: "asyncio.Queue[Tuple[str, Any]]" = asyncio.Queue()
    stop = threading.Event()

    def put(item: Tuple[str, Any]) -> None:
        if not stop.is_set():
            loop.call_soon_threadsafe(queue.put_nowait, item)

    def ask(payload: Dict[str, Any]) -> Optional[int]:
        """Streams one request into the queue; the HTTP status of a refusal."""
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers,
                                         method="POST")
        try:
            opener = _local_live_front_direct_opener if url.startswith("http:") else _local_live_front_opener
            response = opener.open(request, timeout=LOCAL_LIVE_FRONT_TIMEOUT_S)
        except urllib.error.HTTPError as exc:
            exc.close()
            return exc.code
        with response:
            if "json" in (response.headers.get("Content-Type") or "") and \
                    "event-stream" not in (response.headers.get("Content-Type") or ""):
                # A server that ignored "stream": one whole answer.
                put(("whole", response.read(LOCAL_LIVE_FRONT_MAX_LINE_BYTES * 4)))
                return None
            while not stop.is_set():
                line = response.readline(LOCAL_LIVE_FRONT_MAX_LINE_BYTES)
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                if not text.startswith("data:"):
                    continue
                data = text[5:].strip()
                if data == "[DONE]":
                    break
                put(("data", data))
        return None

    def run() -> None:
        try:
            status = ask(body)
            if status == 400 and tools:
                # A server without tool support refuses the request: talk
                # without them rather than not at all.
                logger.info("Local Live's fast model refused tools; answering without them")
                status = ask({k: v for k, v in body.items() if k not in ("tools", "tool_choice")})
            put(("status", status) if status is not None else ("end", None))
        except Exception as exc:  # noqa: BLE001 — any failure ends the stream
            put(("error", type(exc).__name__))

    _local_live_front_executor.submit(run)
    think = _ThinkFilter()
    try:
        while True:
            kind, value = await queue.get()
            if kind == "end":
                rest = think.flush()
                if rest:
                    yield "text", rest
                return
            if kind == "status":
                logger.warning("Local Live's fast model refused the request: HTTP %s", value)
                raise _LocalLiveFrontUnavailable(f"HTTP {value}")
            if kind == "error":
                logger.warning("Local Live couldn't reach its fast model: %s", value)
                raise _LocalLiveFrontUnavailable(value)
            if kind == "whole":
                try:
                    choice = json.loads(value)["choices"][0]["message"]
                except (ValueError, KeyError, IndexError, TypeError):
                    raise _LocalLiveFrontUnavailable("unreadable answer")
                if not isinstance(choice, dict):
                    raise _LocalLiveFrontUnavailable("unreadable answer")
                content = choice.get("content")
                words = think.feed(content if isinstance(content, str) else "") + think.flush()
                if words:
                    yield "text", words
                items = choice.get("tool_calls")
                for index, item in enumerate(items if isinstance(items, list) else []):
                    if not isinstance(item, dict):
                        continue
                    function = item.get("function") if isinstance(item.get("function"), dict) else {}
                    yield "tool_call", {"index": index, "id": item.get("id"), "name": function.get("name"),
                                        "arguments": function.get("arguments") or ""}
                continue
            try:
                chunk = json.loads(value)
                delta = chunk["choices"][0].get("delta") or {}
            except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                continue
            if not isinstance(delta, dict):
                continue
            content = delta.get("content")
            if isinstance(content, str) and content:
                words = think.feed(content)
                if words:
                    yield "text", words
            items = delta.get("tool_calls")
            for item in items if isinstance(items, list) else []:
                if not isinstance(item, dict):
                    continue
                function = item.get("function") if isinstance(item.get("function"), dict) else {}
                index = item.get("index") if isinstance(item.get("index"), int) else 0
                arguments = function.get("arguments")
                yield "tool_call", {"index": index, "id": item.get("id"), "name": function.get("name"),
                                    "arguments": arguments if isinstance(arguments, str)
                                    else (json.dumps(arguments) if arguments is not None else "")}
    finally:
        stop.set()


async def _local_live_front_target(profile: str) -> Optional[Tuple[str, str, str]]:
    """The front model to ask, or None (not set up: Hermes answers each turn)."""
    try:
        return await core._run_scoped(profile, local_live_front_model_target, speech_server._speech_server_executor)
    except core.TokenError:
        return None
    except Exception:  # noqa: BLE001 — a broken config falls back to Hermes turns
        logger.warning("Local Live couldn't read its fast model's settings", exc_info=True)
        return None


@router.get("/local-live/v1/tools")
async def get_local_live_tools(request: Request, response: Response) -> Dict[str, Any]:
    """The tools the profile's live call offers, for an engine that runs its
    own front model."""
    response.headers["Cache-Control"] = "no-store"
    local_live._local_live_start_auth()
    profile = local_live._local_live_request_profile(request)
    local_live._local_live_acquire(profile)
    call = local_live._local_live_active_call(profile)
    agent = getattr(call, "agent", None) if call is not None else None
    return {"in_call": call is not None, "instructions": agent.instructions if agent else None,
            "tools": agent.tools if agent else []}


@router.post("/local-live/v1/tools/call")
async def post_local_live_tool_call(request: Request, response: Response) -> Dict[str, Any]:
    """Runs one of the call's tools on the phone: {name, arguments} ->
    {output}. start_job answers once the job runs; its outcome reaches the
    engine later as a conversation item and response.create on the relay."""
    response.headers["Cache-Control"] = "no-store"
    no_store = {"Cache-Control": "no-store"}
    local_live._local_live_start_auth()
    profile = local_live._local_live_request_profile(request)
    body = await core._read_json_body(request, local_live.LOCAL_LIVE_MAX_BODY_BYTES)
    name = body.get("name") if isinstance(body, dict) else None
    if not isinstance(name, str) or not _LOCAL_LIVE_TOOL_NAME.fullmatch(name):
        raise HTTPException(status_code=400, detail="Missing or invalid 'name'", headers=no_store)
    arguments = body.get("arguments")
    if isinstance(arguments, dict):
        arguments = json.dumps(arguments)
    elif arguments is None:
        arguments = "{}"
    elif not isinstance(arguments, str):
        raise HTTPException(status_code=400, detail="'arguments' must be an object or a JSON string", headers=no_store)
    local_live._local_live_acquire(profile)
    call = local_live._local_live_active_call(profile)
    agent = getattr(call, "agent", None) if call is not None else None
    if call is None or agent is None:
        raise HTTPException(status_code=409, detail=local_live.LOCAL_LIVE_NO_CALL_REPLY, headers=no_store)
    if not any(tool["function"]["name"] == name for tool in agent.tools):
        raise HTTPException(status_code=404, detail=f"This call has no tool named {name}", headers=no_store)
    return {"output": await agent.run_tool(call, name, arguments)}
