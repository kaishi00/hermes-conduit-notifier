"""Ask me first: an agent in a room with other people's agents asks its owner before it shares something personal,
the ``conduit_ask_owner`` tool (designs/cross-owner-agents, N7c).

In a room set to Ask me first, the agent asks its owner's phone ("Laifu asks whether you're free on the 22nd",
with what it would share), and waits a little for the answer: Share, Say instead (what the owner writes), or Don't
share. Unanswered, it tells the room it'll check, and the answer reaches it privately before its next turn in that
room (``frame``, from the turn's framing, heard once that turn replies). Asks are the room's, not the session's, so
a compacted room session's continuation hears them too. Nothing goes into the room by itself.

The ask is a confirm prompt in the profile's present store, so the panel, its notification and its voice yes are
Conduit's own; only a Conduit that says it shows a room's asks gets one (present_store.ROOM_ASK_FEATURE). The tool
is in its own toolset (``conduit_rooms``), which room setup keeps in every room ceiling, and the room rules let it
through only in a room set to Ask me first (room_rules.tool_block). Anywhere else it refuses.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Optional

from hermes_constants import get_hermes_home

from . import client, room_identity, room_rules, session_titles
from .events import push_event
from .present_store import MAX_TITLE_CHARS, PresentError, PresentStore, first_line
from .present_store import _line as clean_line
from .present_store import _text as clean_text

logger = logging.getLogger("hermes.plugins.conduit_push")

TOOL_NAME = room_rules.ASK_TOOL
TOOLSET = "conduit_rooms"
MAX_QUESTION_CHARS = MAX_TITLE_CHARS
MAX_SHARE_CHARS = 500
# How long the call waits for the owner: a room turn holds the room meanwhile.
WAIT_S = 30
POLL_START_S = 0.25
POLL_MAX_S = 1.0

_CHECK_LATER = "Tell the room you'll check with your owner, and don't share it until they've said yes."
UNAVAILABLE = f"Your owner couldn't be asked. {_CHECK_LATER}"

DESCRIPTION = (
    "Ask your owner on their phone before you share something personal about them in this group room (the room is "
    "set to Ask me first): their plans, free time, where they are, what they think, anything about them. question: "
    "what's being asked and by whom, on one line (\"Laifu asks whether you're free on the 22nd\"). share: exactly "
    "what you'd say if they agree. It waits a little for the answer: \"confirmed\" means share exactly that, "
    "\"edit\" means say only what your owner wrote (edit_text), \"declined\" means don't share it, and \"waiting\" "
    "means tell the room you'll check with your owner and share nothing yet; their answer reaches you before your "
    "next turn here. Ask once per question; asking again while one waits returns that one."
)

SCHEMA: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "maxLength": MAX_QUESTION_CHARS,
                         "description": f"What's being asked and by whom, one line (up to {MAX_QUESTION_CHARS} "
                                        "characters)."},
            "share": {"type": "string", "maxLength": MAX_SHARE_CHARS,
                      "description": f"Exactly what you'd share if your owner agrees (up to {MAX_SHARE_CHARS} "
                                     "characters)."},
        },
        "required": ["question", "share"],
    },
}

_NOTES = {
    "waiting": (f"Your owner hasn't answered yet. {_CHECK_LATER} Their answer reaches you before your next turn in "
                "this room."),
    "confirmed": "Your owner said yes: share exactly share (the text they saw), nothing more.",
    "declined": "Your owner said no: don't share it. Say you can't share that.",
    "edit": "Your owner wrote what to say instead (edit_text): say only what your owner wrote, nothing more.",
    "expired": "Your owner didn't answer in time: don't share it.",
    "cancelled": "Taken back: don't share it.",
}


def register(ctx: Any, *, is_child: Callable[[Any], bool], profile: Callable[[], str] = lambda: "default") -> None:
    """Registers the tool, if this Hermes supports plugin tools."""
    if not hasattr(ctx, "register_tool"):
        return

    def handler(args: Any, session_id: Any = None, **_: Any) -> str:
        return handle(args, session_id=session_id, is_child=is_child, profile=profile())

    tool = {"name": TOOL_NAME, "toolset": TOOLSET, "schema": SCHEMA, "handler": handler, "check_fn": available}
    try:
        try:
            ctx.register_tool(**tool, description=SCHEMA["description"], emoji="🙋")
        except TypeError:
            ctx.register_tool(**tool)
    except Exception:  # noqa: BLE001
        logger.warning("conduit_push: could not register %s", TOOL_NAME, exc_info=True)


def available() -> bool:
    """Shown to the model only on an installation that has had a room over a Link: one it hosts or a copy."""
    try:
        root = room_identity.root_home(get_hermes_home())
        return (root / room_identity.DIRECTORY / "rooms.json").exists() or \
            (root / room_identity.DIRECTORY / "copies.db").exists()
    except Exception:  # noqa: BLE001
        return False


def _room_name(room_id: str) -> str:
    """The room's name as the user's rooms list it (room_reading), else "a room"."""
    from . import room_reading

    try:
        home = get_hermes_home()
        rooms = room_reading.Reader(room_identity.root_home(home), home=home).rooms()
    except Exception:  # noqa: BLE001 — the ask still goes, under a plain name
        logger.debug("conduit_push: couldn't read room %s's name for an ask", room_id, exc_info=True)
        return "a room"
    return next((room["name"] for room in rooms if room["room_id"] == room_id), "a room")


def handle(args: Any, *, session_id: Any = None, is_child: Callable[[Any], bool] = lambda _: False,
           room_turn: Optional[Callable[[str], Any]] = None, choices: Optional[Callable[[Any], Any]] = None,
           room_name: Callable[[str], str] = _room_name, store: Optional[PresentStore] = None,
           push: Optional[Callable[[dict], None]] = None, profile: str = "default",
           sleep: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic) -> str:
    try:
        session = session_id.strip() if isinstance(session_id, str) else ""
        return _answer(**_handle(
            args if isinstance(args, dict) else {}, session, is_child,
            room_turn or (lambda sid: room_rules.room_turn(sid, get_hermes_home())),
            choices or (lambda turn: room_rules.choices_for(turn, get_hermes_home())), room_name,
            store or PresentStore(get_hermes_home()), push or client.enqueue, profile, sleep, monotonic))
    except Exception:  # noqa: BLE001 — a tool must never raise
        logger.warning("conduit_ask_owner failed", exc_info=True)
        return _answer(ok=False, error="unavailable", note=UNAVAILABLE)


def _handle(args: dict[str, Any], session: str, is_child: Callable[[Any], bool], room_turn: Callable[[str], Any],
            choices: Callable[[Any], Any], room_name: Callable[[str], str], store: PresentStore,
            push: Callable[[dict], None], profile: str, sleep: Callable[[float], None],
            monotonic: Callable[[], float]) -> dict[str, Any]:
    if is_child(session):
        return {"ok": False, "error": "subagent",
                "note": "Only the room's own turn can ask your owner. Report back; the main agent can ask."}
    turn = room_turn(session) if session else None
    if turn is not None and turn.with_others and not turn.room_id:
        # A member's session Hermes hasn't titled with its room yet: a room all the same.
        return {"ok": False, "error": "unavailable", "note": UNAVAILABLE}
    if turn is None or not turn.with_others:
        return {"ok": False, "error": "not_room",
                "note": "This asks your owner from a group room with other people's agents; this isn't one. In "
                        "a private chat, ask the user directly."}
    if choices(turn).share != "ask":
        return {"ok": False, "error": "not_ask_first", "note": room_rules.ASK_BLOCK}
    question = clean_line(args.get("question"), MAX_QUESTION_CHARS + 1)
    share = clean_text(args.get("share"), MAX_SHARE_CHARS + 1)
    if not question:
        return {"ok": False, "error": "no_question", "note": "Give the question on one line, and who asks it."}
    if not share:
        return {"ok": False, "error": "no_share", "note": "Give exactly what you'd share if your owner agrees."}
    if len(question) > MAX_QUESTION_CHARS:
        return {"ok": False, "error": "too_long", "note": f"question is at most {MAX_QUESTION_CHARS} characters."}
    if len(share) > MAX_SHARE_CHARS:
        return {"ok": False, "error": "too_long", "note": f"share is at most {MAX_SHARE_CHARS} characters."}
    name = room_name(turn.room_id)
    try:
        prompt, new = store.ask_room(question, share, room_id=turn.room_id, room_name=name, session_id=session)
    except PresentError as error:
        return {"ok": False, "error": error.code, "note": f"Your owner couldn't be asked ({error}). {_CHECK_LATER}"}
    if new:
        _nudge(prompt, f"{name}: {first_line(share)}", push, profile)
        prompt = _wait(store, prompt, sleep, monotonic)
    else:
        prompt = store.room_ask(prompt["id"]) or prompt
    return {"ok": True, **_with_note(prompt)}


def _wait(store: PresentStore, prompt: dict[str, Any], sleep: Callable[[float], None],
          monotonic: Callable[[], float]) -> dict[str, Any]:
    """Polls the store (the phone's answer arrives through the dashboard, another process) until the ask is answered
    or WAIT_S is up."""
    deadline = monotonic() + WAIT_S
    delay = POLL_START_S
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            return prompt
        sleep(min(delay, remaining))
        delay = min(delay * 1.5, POLL_MAX_S)
        current = store.get(prompt["id"])
        if current is None:
            return prompt
        if current["status"] != "waiting":
            # Returned to the agent now, so its next turn isn't told again.
            return store.room_ask(prompt["id"]) or current
        prompt = current


def _nudge(prompt: dict[str, Any], body: str, push: Callable[[dict], None], profile: str) -> None:
    """Wakes the phone through the relay. Never fails the tool: a phone that misses it picks the ask up on its next
    exchange."""
    try:
        push(push_event("phone.changes", identifier=f"present:{prompt['id']}", profile=profile,
                        profile_name=session_titles.bot_name(), title=prompt["title"], body=body, mode="alert"))
    except Exception:  # noqa: BLE001
        logger.warning("conduit_push: could not wake the phone for a room's ask", exc_info=True)


# Answers framed into a room session's turn, marked heard when it replies (heard).
_framed: dict[str, list[str]] = {}
MAX_FRAMED = 256


def frame(store: PresentStore, room_id: Any, session_id: str) -> list[str]:
    """The answers for a turn in ``room_id`` now starting in ``session_id``, left unheard until the turn replies, so
    a turn that fails before it reads them frames them again."""
    found = answers(store, room_id)
    if found:
        _framed.pop(session_id, None)
        _framed[session_id] = [prompt_id for prompt_id, _ in found]
        while len(_framed) > MAX_FRAMED:
            _framed.pop(next(iter(_framed)))
    return [line for _, line in found]


def heard(store_for: Callable[[], PresentStore], session_id: str, replied: bool) -> None:
    """A turn in ``session_id`` ended: what it was framed with is heard once it replied."""
    if not replied or session_id not in _framed:
        return
    store_for().mark_heard(_framed.pop(session_id))


def answers(store: PresentStore, room_id: Any) -> list[tuple[str, str]]:
    """The room's answers its agent hasn't heard: each prompt's id and its private line."""
    lines = []
    for prompt in store.room_answers(room_id):
        question = prompt["title"]
        if prompt["status"] == "confirmed":
            lines.append((prompt["id"], f'Your owner said yes to "{question}": you may share this, and nothing more: '
                         f'{_one_line(prompt["preview"])}'))
        elif prompt["status"] == "edit" and prompt.get("edit_text"):
            lines.append((prompt["id"], f'Your owner answered "{question}" with: {_one_line(prompt["edit_text"])} '
                                        'Share only that.'))
        elif prompt["status"] == "expired":
            lines.append((prompt["id"], f'Your owner didn\'t answer "{question}" in time: don\'t share it.'))
        else:
            lines.append((prompt["id"], f'Your owner said no to "{question}": don\'t share it.'))
    return lines


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


def _with_note(prompt: dict[str, Any]) -> dict[str, Any]:
    shown = {key: value for key, value in prompt.items() if key not in ("device", "finished_at", "room", "preview")}
    if "preview" in prompt:
        shown["share"] = prompt["preview"]
    status = prompt["status"]
    if status == "edit" and not prompt.get("edit_text"):
        status = shown["status"] = "declined"  # Say instead with nothing to say
    shown["note"] = _NOTES[status]
    return shown


def _answer(**fields: Any) -> str:
    return json.dumps(fields, ensure_ascii=False)
