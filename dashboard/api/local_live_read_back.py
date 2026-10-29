"""Local Live on a read-back speech server."""

from __future__ import annotations

import asyncio
import difflib
import json
import re
import time
import uuid
from collections import deque
import contextlib
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from . import core, local_live, watch_local_live

logger = core.logger


# --- Local Live on a read-back speech server ----------------------------------
#
# Some speech servers only listen and read aloud: a response reads back the
# user text it's given, with no model behind it (what Classic voice's speak
# socket relies on), and a session that asks for automatic responses
# (turn_detection.create_response true) is refused. The farm's fleet voice
# adapter is one. Such a server never asks the model address for a reply, so
# on a Local Live call the plugin drives the turns itself, on the call's relay:
#
#   server: conversation.item.input_audio_transcription.completed
#     -> the call's turn (call.start_turn), as the model address starts one
#   the turn's reply, as it streams, in sentences
#     -> one read-back response.create per sentence (inline input)
#   server: input_audio_buffer.speech_started -> the reply stops (barge-in)
#
# The call's own side doesn't change: the phone's or the Watch's
# LocalLiveSession sends the same session.update and readiness check, and
# sees one response per reply, as from a server that answers by itself. The
# plugin finds out from the server's answer to that session.update: a server
# that refuses create_response for that reason gets the same session.update
# with automatic responses off, and the call goes on in read-back.

# A sentence goes to the server once this long (the first of a reply at any
# length, so the reply starts sooner); shorter ones join the next.
LOCAL_LIVE_READ_BACK_SENTENCE_CHARS = 40
LOCAL_LIVE_READ_BACK_MAX_SENTENCE_CHARS = 400
# Text that waits this long with no sentence end goes anyway.
LOCAL_LIVE_READ_BACK_FLUSH_S = 1.2
LOCAL_LIVE_READ_BACK_TICK_S = 0.25
# A response the server goes quiet on this long (not started, or no audio
# since) is given up on.
LOCAL_LIVE_READ_BACK_RESPONSE_TIMEOUT_S = 30.0
# A cancelled response the server says nothing more about in this long is
# over: a server may drop one it hadn't started without a word.
LOCAL_LIVE_READ_BACK_CANCEL_TIMEOUT_S = 2.0
# Speech heard before the reply says anything holds its first sentence until
# the words are transcribed, at most this long.
LOCAL_LIVE_READ_BACK_HOLD_S = 10.0
# A sentence whose spoken transcript matches it this little was answered by a
# server with a model of its own; sentences shorter than this many words (or
# with digits, which a server may say in words) don't count, and it takes
# this many misses in a row to end the call.
LOCAL_LIVE_READ_BACK_MIN_MATCH = 0.6
LOCAL_LIVE_READ_BACK_CHECK_WORDS = 5
LOCAL_LIVE_READ_BACK_MISSES = 2
LOCAL_LIVE_NOT_READ_BACK = ("Your speech server doesn't read text back as it's given, "
                            "so it can't speak Hermes' replies in a Local Live call")
# How a read-back server refuses automatic responses: the error's code, or
# the session field it names.
_LOCAL_LIVE_READ_BACK_REFUSAL_CODES = ("realtime_live_unavailable",)
_LOCAL_LIVE_READ_BACK_REFUSAL_PARAMS = ("session.audio.input.turn_detection.create_response",
                                        "session.turn_detection.create_response")
_LOCAL_LIVE_READ_BACK_AUDIO = ('"response.output_audio.delta"', '"response.audio.delta"')
_LOCAL_LIVE_READ_BACK_CLIENT_KINDS = ('"session.update"', '"conversation.item.create"', '"response.create"',
                                      '"response.cancel"')
_LOCAL_LIVE_READ_BACK_TRANSCRIPT_DELTAS = ("response.output_audio_transcript.delta", "response.audio_transcript.delta")
_LOCAL_LIVE_READ_BACK_TRANSCRIPT_DONE = ("response.output_audio_transcript.done", "response.audio_transcript.done")
_LOCAL_LIVE_READ_BACK_WORD = re.compile(r"[\w']+")


class _ReadBackSentences:
    """A reply's spoken text, as it streams, in sentences for a read-back
    server: the first as soon as it ends, the rest at least
    LOCAL_LIVE_READ_BACK_SENTENCE_CHARS long (shorter ones join the next),
    none past LOCAL_LIVE_READ_BACK_MAX_SENTENCE_CHARS. The text is already
    what's to be said (the phone's and the Watch's turns leave out what's
    written to be seen)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.raw = ""
        self.held = ""
        self.fed_at: Optional[float] = None
        self.emitted = 0

    def feed(self, text: str) -> List[str]:
        self.fed_at = self.clock()
        self.raw += text
        out: List[str] = []
        limit = LOCAL_LIVE_READ_BACK_MAX_SENTENCE_CHARS
        while True:
            match = watch_local_live._LOCAL_LIVE_WATCH_BOUNDARY.search(self.raw)
            if match is not None and match.end() <= limit:
                chunk, self.raw = self.raw[:match.end()], self.raw[match.end():]
                self._take(chunk, out)
            elif len(self.raw) > limit:
                # A sentence too long to wait for its end: cut on a space.
                cut = self.raw.rfind(" ", 0, limit)
                cut = cut if cut > 0 else limit
                chunk, self.raw = self.raw[:cut], self.raw[cut:]
                self._take(chunk, out, whole=True)
            else:
                break
        return out

    def tick(self) -> List[str]:
        """What has waited LOCAL_LIVE_READ_BACK_FLUSH_S since the last text
        with no sentence end (asked only while nothing else is to be said)."""
        out: List[str] = []
        if self.fed_at is None or self.clock() - self.fed_at < LOCAL_LIVE_READ_BACK_FLUSH_S:
            return out
        cut = self.raw.rfind(" ")
        if cut > 0:
            chunk, self.raw = self.raw[:cut], self.raw[cut:]
            self._take(chunk, out, whole=True)
        self._release(out)
        self.fed_at = None
        return out

    def finish(self) -> List[str]:
        out: List[str] = []
        if self.raw:
            chunk, self.raw = self.raw, ""
            self._take(chunk, out, whole=True)
        self._release(out)
        self.fed_at = None
        return out

    def _release(self, out: List[str]) -> None:
        if self.held:
            out.append(self.held)
            self.held = ""
            self.emitted += 1

    def _take(self, chunk: str, out: List[str], whole: bool = False) -> None:
        text = " ".join(chunk.split())
        if not text:
            return
        self.held = f"{self.held} {text}" if self.held else text
        wanted = 1 if self.emitted == 0 else LOCAL_LIVE_READ_BACK_SENTENCE_CHARS
        ends = whole or self.held[-1] in ".!?…:;\"')]"
        if (len(self.held) >= wanted and ends) or len(self.held) >= LOCAL_LIVE_READ_BACK_MAX_SENTENCE_CHARS:
            self._release(out)


def _local_live_read_back_matches(said: str, asked: str) -> Optional[bool]:
    """Whether the server's transcript of a response is the sentence it was
    given; None when the sentence can't tell (short, or with digits)."""
    said_words = _LOCAL_LIVE_READ_BACK_WORD.findall(said.lower())
    asked_words = _LOCAL_LIVE_READ_BACK_WORD.findall(asked.lower())
    if len(asked_words) < LOCAL_LIVE_READ_BACK_CHECK_WORDS or any(ch.isdigit() for ch in asked):
        return None
    return difflib.SequenceMatcher(None, said_words, asked_words).ratio() >= LOCAL_LIVE_READ_BACK_MIN_MATCH


def _local_live_turn_detections(update: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The session.update's turn detection blocks (the GA one and the beta
    one) that ask for automatic responses."""
    session = update.get("session")
    if not isinstance(session, dict):
        return []
    audio = session.get("audio")
    audio_input = audio.get("input") if isinstance(audio, dict) else None
    blocks = (audio_input.get("turn_detection") if isinstance(audio_input, dict) else None,
              session.get("turn_detection"))
    return [block for block in blocks if isinstance(block, dict) and block.get("create_response", True) is not False]


def _local_live_turn_detection(update: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The first turn detection that asks for automatic responses; None when none does."""
    detections = _local_live_turn_detections(update)
    return detections[0] if detections else None


def _local_live_read_back_update(update: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    """The call's session.update with automatic responses off, and whether
    it wanted the reply to stop when the user speaks."""
    copy = json.loads(json.dumps(update))
    detections = _local_live_turn_detections(copy)
    interrupts = all(block.get("interrupt_response", True) is not False for block in detections)
    for block in detections:
        block["create_response"] = False
        block["interrupt_response"] = False
    return copy, interrupts


def _local_live_refuses_live(event: Dict[str, Any]) -> bool:
    """Whether an error is a read-back server refusing automatic responses."""
    error = event.get("error") if isinstance(event.get("error"), dict) else event
    return (error.get("code") in _LOCAL_LIVE_READ_BACK_REFUSAL_CODES
            or error.get("param") in _LOCAL_LIVE_READ_BACK_REFUSAL_PARAMS)


def _local_live_item_words(item: Any) -> Optional[str]:
    """A user message item's text (joined as _local_live_user_text joins a
    request's); None for any other item."""
    if not isinstance(item, dict) or item.get("role") != "user":
        return None
    content = item.get("content")
    if not isinstance(content, list):
        return None
    parts = [part["text"] for part in content if isinstance(part, dict)
             and part.get("type") in ("text", "input_text") and isinstance(part.get("text"), str)]
    return " ".join(" ".join(parts).split())[:local_live.LOCAL_LIVE_MAX_TURN_CHARS]


def _local_live_read_back_response(status: Optional[str], response_id: str) -> str:
    """A response event for the call: created (status None) or done."""
    response: Dict[str, Any] = {"id": response_id, "object": "realtime.response",
                                "status": status or "in_progress", "output": []}
    return json.dumps({"type": "response.done" if status else "response.created", "response": response})


class _LocalLiveReadBack:
    """One Local Live connection's turns on a read-back speech server, run by
    the plugin between the call and the server (see above).

    ``from_client`` and ``from_server`` see each text frame first; a frame
    either goes on (unchanged, or changed) or stays here. ``to_client`` and
    ``to_server`` send frames of its own, in order with the relayed ones;
    ``fail`` ends the call with a reason. Everything runs on the relay's loop.
    """

    def __init__(self, call: Any, to_client: Callable[[str], Awaitable[None]],
                 fail: Callable[[str], Awaitable[None]], clock: Callable[[], float] = time.monotonic) -> None:
        self.call = call
        self.to_client = to_client
        self.fail = fail
        self.clock = clock
        self.to_server: Optional[Callable[[str], Awaitable[None]]] = None
        self.close_server: Optional[Callable[[], Awaitable[None]]] = None
        # Read-back is on: the server refused automatic responses.
        self.on = False
        # The call's session.update, until the server answers it.
        self.update: Optional[Dict[str, Any]] = None
        self.interrupts = True
        # A typed turn's words, until its response.create.
        self.typed: Optional[str] = None
        # The call's open turn, and the task speaking its reply.
        self.turn: Any = None
        self.follower: Optional["asyncio.Task[None]"] = None
        # The open turn's reply is all queued (or there's no turn).
        self.turn_over = True
        self.sentences: deque = deque()
        # The sentence of the server's response in progress, and whether the
        # server has started it.
        self.speaking: Optional[str] = None
        self.started = False
        # When the server last sent anything for it.
        self.heard_at = 0.0
        # That response was cancelled: its audio and transcript stay here.
        self.muting = False
        # The server is busy with a response this didn't ask for: the next
        # sentence waits for its end.
        self.blocked = False
        self.watchdog: Optional[asyncio.TimerHandle] = None
        # The reply the call sees: open from its turn until it ends, as one
        # response however many sentences it takes.
        self.reply_open = False
        self.reply_id: Optional[str] = None
        self.reply_sentences = 0
        # Something of the reply went to the server: speech over it stops it.
        self.reply_said = False
        # The user started speaking before the reply said anything: it waits
        # for their words (or for nothing to come of them).
        self.held = False
        self.hold_timer: Optional[asyncio.TimerHandle] = None
        # The call ended here (the server isn't reading back): nothing more
        # of the server's goes on.
        self.ended = False
        # The response in progress is the reply's second sentence or later:
        # its transcript starts with a space.
        self.spaced = False
        self.misses = 0
        # Bumped by each new turn: a start that another overtook while it
        # waited for the call lets its own turn go.
        self.generation = 0
        self.closed = False

    def attach(self, to_server: Callable[[str], Awaitable[None]],
               close_server: Optional[Callable[[], Awaitable[None]]] = None) -> None:
        self.to_server = to_server
        self.close_server = close_server

    def close(self) -> None:
        self.closed = True
        self._release()
        if self.follower is not None:
            self.follower.cancel()
            self.follower = None
        if self.watchdog is not None:
            self.watchdog.cancel()
            self.watchdog = None

    # The call's frames.

    async def from_client(self, text: str) -> bool:
        """True for a frame that stays here (a turn this runs instead)."""
        if self.closed or not any(kind in text for kind in _LOCAL_LIVE_READ_BACK_CLIENT_KINDS):
            return False
        try:
            frame = json.loads(text)
        except ValueError:
            return False
        kind = frame.get("type") if isinstance(frame, dict) else None
        if kind == "session.update":
            if self.on:
                await self._send_update(frame)
                return True
            if _local_live_turn_detection(frame) is not None:
                self.update = frame
            return False
        if not self.on:
            return False
        if kind == "conversation.item.create":
            words = _local_live_item_words(frame.get("item"))
            if words is None:
                return False
            # A typed turn: its response.create follows.
            self.typed = words
            return True
        if kind == "response.create":
            response = frame.get("response") if isinstance(frame.get("response"), dict) else {}
            inline = response.get("input")
            if isinstance(inline, list):
                # The readiness check's marker, asked out of band.
                words = " ".join(word for word in (_local_live_item_words(item) for item in inline) if word)
            else:
                words = self.typed or ""
            self.typed = None
            await self._start(words[:local_live.LOCAL_LIVE_MAX_TURN_CHARS])
            return True
        if kind == "response.cancel":
            # The user stopped the reply on the phone. With no sentence in
            # progress here, the call's cancel still goes on: one the server
            # finished may still be playing, and a server that shuts its
            # microphone until its audio has played opens it again (#1325).
            return await self._stop(cancel_turn=True)
        return False

    # The server's frames.

    async def from_server(self, text: str) -> Optional[str]:
        """The frame to pass on to the call, or None for one that stays here."""
        if self.ended:
            return None
        if self.closed:
            return text
        if any(kind in text for kind in _LOCAL_LIVE_READ_BACK_AUDIO):
            if not self.on:
                return text
            if self.muting or self.speaking is None:
                return None
            self.heard_at = self.clock()
            return text
        if not self.on and self.update is None:
            return text
        try:
            event = json.loads(text)
        except ValueError:
            return text
        kind = event.get("type") if isinstance(event, dict) else None
        if self.update is not None:
            if kind == "error" and _local_live_refuses_live(event):
                update, self.update = self.update, None
                self.on = True
                logger.info("Local Live: the speech server only reads text back; the plugin runs the call's turns")
                await self._send_update(update)
                return None
            if kind in ("session.updated", "error"):
                self.update = None
        if not self.on:
            return text
        if kind == "conversation.item.input_audio_transcription.completed":
            words = " ".join(str(event.get("transcript") or "").split())
            # The call shows the words before Hermes' turn for them starts.
            await self._tell(text)
            if words:
                await self._start(words[:local_live.LOCAL_LIVE_MAX_TURN_CHARS])
            elif self.held:
                # Nothing came of the sound: the reply goes on.
                self._release()
                await self._pump()
            return None
        if kind == "conversation.item.input_audio_transcription.failed" and self.held:
            await self._tell(text)
            self._release()
            await self._pump()
            return None
        if kind == "input_audio_buffer.speech_started":
            await self._tell(text)
            # The user talks over the reply: it stops, and its turn goes. While
            # nothing of it has been said yet (Hermes still at work), only
            # words make a new turn: a cough doesn't cost the reply.
            if self.interrupts and self.reply_open:
                if (self.speaking is not None and not self.muting) or self.reply_said:
                    await self._stop(cancel_turn=True)
                else:
                    self._hold()
            return None
        if kind == "response.created":
            if self.speaking is None or self.muting:
                if self.speaking is not None:
                    self.started = True
                    # Cancelled before it started: its end should follow soon.
                    self._arm_watchdog(LOCAL_LIVE_READ_BACK_CANCEL_TIMEOUT_S)
                return None
            self.started = True
            self.heard_at = self.clock()
            if self.reply_id is None:
                response = event.get("response") if isinstance(event.get("response"), dict) else {}
                self.reply_id = str(response.get("id") or f"resp_conduit_{uuid.uuid4().hex[:16]}")
                return text
            return None
        if kind in _LOCAL_LIVE_READ_BACK_TRANSCRIPT_DELTAS:
            if self.muting or self.speaking is None:
                return None
            self.heard_at = self.clock()
            if self.spaced and isinstance(event.get("delta"), str):
                self.spaced = False
                event["delta"] = " " + event["delta"]
                return json.dumps(event)
            return text
        if kind in _LOCAL_LIVE_READ_BACK_TRANSCRIPT_DONE:
            if self.muting or self.speaking is None:
                return None
            said = event.get("transcript")
            if isinstance(said, str):
                verdict = _local_live_read_back_matches(said, self.speaking)
                if verdict is False:
                    self.misses += 1
                    if self.misses >= LOCAL_LIVE_READ_BACK_MISSES:
                        logger.warning("Local Live: the speech server didn't read Hermes' reply back; ending the call")
                        self.ended = True
                        self.close()
                        await self.fail(LOCAL_LIVE_NOT_READ_BACK)
                        # The relay ends with the server's side.
                        if self.close_server is not None:
                            with contextlib.suppress(Exception):
                                await self.close_server()
                        return None
                elif verdict:
                    self.misses = 0
                if self.reply_sentences > 1:
                    event["transcript"] = " " + said
                    return json.dumps(event)
            return text
        if kind == "response.done":
            self._response_over()
            await self._pump()
            return None
        if kind == "error":
            error = event.get("error") if isinstance(event.get("error"), dict) else {}
            code = str(error.get("code") or "")
            if "cancel" in code:
                # A cancel that found nothing running (a server may say so).
                return None
            if self.speaking is not None and not self.started:
                # The server's answer to the response.create just sent.
                busy = code == "conversation_already_has_active_response"
                if busy and not self.muting:
                    self.sentences.appendleft(self.speaking)
                    # Counted again when it's sent again.
                    self.reply_sentences -= 1
                elif not busy:
                    logger.info("Local Live: the speech server refused a read-back sentence (%s)",
                                (code or "error")[:64])
                self._response_over()
                if busy:
                    # Its own response's end frees it; or this gives up waiting.
                    self.blocked = True
                    self._arm_watchdog(LOCAL_LIVE_READ_BACK_RESPONSE_TIMEOUT_S)
                await self._pump()
                return None
            if self.muting:
                return None
            return text
        if kind in ("response.output_item.added", "response.output_item.done", "response.output_audio.done",
                    "response.audio.done", "response.content_part.added", "response.content_part.done"):
            return None if self.muting or self.speaking is None else text
        return text

    # Turns.

    async def _start(self, words: str) -> None:
        """A new turn for ``words``: the reply before it stops being spoken
        (the call's start_turn lets its turn go)."""
        await self._stop(cancel_turn=False)
        generation = self.generation
        self.reply_open = True
        self.turn_over = False
        turn = await self.call.start_turn(words) if words else None
        if self.closed or generation != self.generation:
            if turn is not None:
                await self.call.cancel_turn(turn)
            return
        if turn is None:
            self.turn_over = True
            await self._pump()
            return
        self.turn = turn
        follower = self.follower = asyncio.ensure_future(self._follow(turn))
        local_live._local_live_tasks.add(follower)
        follower.add_done_callback(local_live._local_live_task_done)

    async def _stop(self, cancel_turn: bool) -> bool:
        """The reply stops: nothing more of it is said. True when the server
        was sent a response.cancel for it, now or before (a send that failed
        counts too: the relay ends with a broken upstream anyway)."""
        # A start still waiting on the call is overtaken.
        self.generation += 1
        self._release()
        if self.follower is not None:
            self.follower.cancel()
            self.follower = None
        self.sentences.clear()
        # A response already being cancelled was told before.
        cancelled = self.muting
        if self.speaking is not None and not self.muting:
            cancelled = True
            self.muting = True
            # A server may drop a response it hadn't started without a word.
            self._arm_watchdog(LOCAL_LIVE_READ_BACK_CANCEL_TIMEOUT_S if not self.started
                               else LOCAL_LIVE_READ_BACK_RESPONSE_TIMEOUT_S)
            await self._send_server(json.dumps({"type": "response.cancel"}))
        turn, self.turn = self.turn, None
        self.turn_over = True
        if cancel_turn and turn is not None:
            await self.call.cancel_turn(turn)
        await self._end_reply("cancelled")
        return cancelled

    async def _follow(self, turn: Any) -> None:
        """The turn's reply, sentence by sentence, as it streams."""
        sentences = _ReadBackSentences(self.clock)
        deadline = self.clock() + local_live.LOCAL_LIVE_TURN_MAX_S
        heard = self.clock()
        while True:
            now = self.clock()
            if now >= deadline or now - heard >= local_live.LOCAL_LIVE_TURN_IDLE_S:
                break
            try:
                kind, delta = await asyncio.wait_for(turn.frames.get(), LOCAL_LIVE_READ_BACK_TICK_S)
            except asyncio.TimeoutError:
                if self.speaking is None and not self.sentences:
                    await self._queue(sentences.tick())
                continue
            if kind == "done":
                break
            heard = self.clock()
            await self._queue(sentences.feed(delta))
        await self._queue(sentences.finish())
        if self.turn is turn:
            self.turn = None
            self.follower = None
            self.turn_over = True
            # A turn that timed out is let go; a finished one is gone already.
            await self.call.cancel_turn(turn)
            await self._pump()

    async def _queue(self, sentences: List[str]) -> None:
        if sentences:
            self.sentences.extend(sentences)
            await self._pump()

    async def _pump(self) -> None:
        """The next sentence to the server once it's free, or the reply's end."""
        if self.closed or self.speaking is not None or self.blocked or self.held:
            return
        if self.sentences:
            sentence = self.sentences.popleft()
            self.speaking, self.started = sentence, False
            self.reply_sentences += 1
            self.reply_said = True
            self.spaced = self.reply_sentences > 1
            self.heard_at = self.clock()
            self._arm_watchdog(LOCAL_LIVE_READ_BACK_RESPONSE_TIMEOUT_S)
            item = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": sentence}]}
            await self._send_server(json.dumps({"type": "response.create",
                                                "response": {"conversation": "none", "input": [item]}}))
            return
        if self.turn_over:
            await self._end_reply("completed")

    async def _end_reply(self, status: str) -> None:
        if not self.reply_open:
            return
        self.reply_open = False
        reply_id, self.reply_id = self.reply_id, None
        self.reply_sentences = 0
        self.reply_said = False
        self.spaced = False
        frames = []
        if reply_id is None:
            # Nothing was said (an empty reply, or the readiness check's):
            # the call still sees a response begin and end.
            reply_id = f"resp_conduit_{uuid.uuid4().hex[:16]}"
            frames.append(_local_live_read_back_response(None, reply_id))
        frames.append(_local_live_read_back_response(status, reply_id))
        # Both or neither, even when the task saying them is cancelled.
        await asyncio.shield(self._tell(*frames))

    async def _tell(self, *frames: str) -> None:
        """Frames for the call; a call that's gone hears them no more."""
        for frame in frames:
            try:
                await self.to_client(frame)
            except Exception:  # noqa: BLE001 — the relay sees the closed socket itself
                return

    def _hold(self) -> None:
        """Holds the reply's first sentence while the user may be speaking."""
        self._release()
        self.held = True

        def expire() -> None:
            self.hold_timer = None
            if self.held and not self.closed:
                self.held = False
                local_live._local_live_spawn(self._pump())

        self.hold_timer = asyncio.get_running_loop().call_later(LOCAL_LIVE_READ_BACK_HOLD_S, expire)

    def _release(self) -> None:
        self.held = False
        if self.hold_timer is not None:
            self.hold_timer.cancel()
            self.hold_timer = None

    def _response_over(self) -> None:
        self.speaking, self.started = None, False
        self.muting = False
        self.blocked = False
        if self.watchdog is not None:
            self.watchdog.cancel()
            self.watchdog = None

    def _arm_watchdog(self, timeout: float) -> None:
        """Gives up on the server's response (or its being busy) once it has
        gone quiet for ``timeout``: a response still sending audio isn't
        quiet, however long its sentence."""
        if self.watchdog is not None:
            self.watchdog.cancel()

        def expire() -> None:
            self.watchdog = None
            if self.closed:
                return
            quiet = self.clock() - self.heard_at
            if self.started and not self.muting and not self.blocked and quiet < timeout:
                self.watchdog = asyncio.get_running_loop().call_later(timeout - quiet, expire)
                return
            logger.info("Local Live: the speech server went quiet on a read-back response; going on")
            self._response_over()
            local_live._local_live_spawn(self._pump())

        self.watchdog = asyncio.get_running_loop().call_later(timeout, expire)

    async def _send_update(self, update: Dict[str, Any]) -> None:
        changed, self.interrupts = _local_live_read_back_update(update)
        await self._send_server(json.dumps(changed))

    async def _send_server(self, text: str) -> None:
        if self.to_server is not None and not self.closed:
            with contextlib.suppress(Exception):
                await self.to_server(text)
