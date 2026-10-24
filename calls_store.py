"""Hermes calls you (#449): this profile's call settings and job watches.

A watch says "call the user when this job ends". Conduit registers one as
soon as the user asks a Live Voice call for it ("call me when it's done"),
keyed to the job's own Hermes session ids (runtime and stored), never to the
voice conversation. The agent's turn-end hooks fire it: the first turn end of
any of its sessions consumes it, under the file lock, so a replayed hook finds
nothing and the user is called at most once per watch.

While the call is still going the watch is held: Conduit renews a short hold
during the call and releases it at hang-up. A job that ends while held is
told in the call itself (Conduit then removes the watch), or, when the call
ends without that, at release (Conduit tells the user itself) or once the
hold runs out because the phone went away (the hooks' delivery calls then).
So the call never depends on the phone reaching the host after hanging up.

Hermes can also ask for a call itself (the ``conduit_call_user`` tool): a
watch on the asking session that calls when its turn ends, with the reason
Hermes gave. With "approval, question and failure calls" on, a request for
the user's approval or answer that goes unanswered for a minute calls too
(an alert: a watch that is already due), and so does a failed turn.

While the user is in a Live Voice call (Conduit renews a presence hold),
nothing rings: a call that would go out then sends the usual push instead.

A call the user declines, or that goes unanswered, comes back from Conduit
as an outcome on the call's sessions; the next turn of one of them takes it,
so Hermes hears it once (``calls.outcome_note``).

While an alert can call, what Hermes waits on is kept too: a question
batch's choices, or an approval's command and description. A call answered
on the Apple Watch, with no chat open, reads them from here and leaves the
user's spoken answer, which the agent's process passes on to the waiting
clarify loop or approval (``answer_waiting``, ``take_answers``,
``take_approval``).

State lives in the profile's ``conduit-calls.json`` beside the pairing state,
so watches survive gateway and dashboard restarts. The hooks (agent process)
and the dashboard routes (dashboard process) both use this module: the
dashboard loads it by path, so it imports nothing from the plugin package.
"""

from __future__ import annotations

import hashlib
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

FILE_NAME = "conduit-calls.json"
VERSION = 1

DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": False,
    # Call when the user asked for it ("call me when it's done").
    "when_asked": True,
    # Hermes may call on its own judgment (the conduit_call_user tool).
    "decides": False,
    # Call when Hermes waits on the user's approval or answer, or a turn fails.
    "alerts": False,
    "min_gap_s": 120,
    "per_hour": 6,
    "per_day": 20,
    # The user's own words on what's worth a call Hermes decides to make
    # ("only if production is down"): Hermes reads them before such a call
    # (call_tool). Empty: the skill's rule alone.
    "rules": "",
    # Saved places (Conduit's ids, location sharing) where the phone must
    # not ring: a call then sends the usual notification.
    # avoid_unknown_place: also hold calls while Conduit can't say where the
    # user is (no recent location, or sharing paused or off).
    "avoid_places": [],
    "avoid_unknown_place": False,
}
BOOL_SETTINGS = ("enabled", "when_asked", "decides", "alerts", "avoid_unknown_place")
MAX_AVOID_PLACES = 20
_PLACE_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}\Z")
MAX_RULES_CHARS = 500
# Inclusive bounds. per_day stops at the relay's own daily ceiling.
INT_BOUNDS: dict[str, tuple[int, int]] = {
    "min_gap_s": (30, 3600),
    "per_hour": (1, 30),
    "per_day": (1, 60),
}

OUTCOMES = ("done", "failed", "stopped")
# What an alert calls about: Hermes waits on the user.
ALERT_KINDS = ("approval", "question")
# Who asked for the watch: Conduit (the user, in a call), Hermes' tool, or an
# alert.
ORIGINS = ("conduit", "tool", "alert")
# An approval or question the user hasn't answered in this long calls.
ALERT_DELAY_S = 60
MAX_REASON_CHARS = 200
WATCH_TTL_S = 24 * 3600
MAX_WATCHES = 20
MAX_WATCH_SESSIONS = 4
MAX_TITLE_CHARS = 120
# A hold covers a running call between Conduit's renewals.
MAX_HOLD_S = 600
# Turn ends remembered so a watch that arrives just after its job ended still
# calls (the user hung up as the job finished). Only kept while calls are on.
RECENT_END_TTL_S = 30 * 60
MAX_RECENT_ENDS = 64
# Call times kept for the limits: no more than the highest daily limit allows.
MAX_HISTORY = INT_BOUNDS["per_day"][1]
HOUR_S = 3600
DAY_S = 24 * 3600
# Failed turns' decisions by seeded call id, for a replayed hook: kept as long
# as the relay remembers an event id.
MAX_SEEDED = 64
# How a call that rang ended without the user answering, as Conduit reports
# it: declined, or missed (rang out, silenced by Do Not Disturb, or reached a
# phone that was offline).
CALL_OUTCOMES = ("declined", "missed")
# What a call is about (the call request's kind).
CALL_KINDS = (*OUTCOMES, *ALERT_KINDS)
# Outcomes waiting for their session's next turn: older ones say nothing
# worth saying.
OUTCOME_TTL_S = 24 * 3600
MAX_OUTCOMES = 20
# Calls whose outcome a turn has taken, so a late retry of the report isn't
# heard twice: kept as long as an outcome could still arrive.
MAX_TOLD = 64
# What Hermes waits on in a chat while an alert can call about it, so a call
# answered on the Apple Watch, with no chat open, can read the choices or the
# command and answer them (hermes-conduit designs/hermes-call-clarify.md).
# Kept only while alert calls are on, and gone once answered elsewhere.
WAITING_TTL_S = 2 * 3600
MAX_WAITING = 10
MAX_WAITING_QUESTIONS = 8
MAX_WAITING_CHOICES = 12
MAX_WAITING_TEXT_CHARS = 600
MAX_WAITING_CHOICE_CHARS = 200
MAX_WAITING_COMMAND_CHARS = 600
MAX_WAITING_ANSWER_CHARS = 2000
# A kept answer: a multi-select's JSON list of choices can outgrow the
# words' limit, and a cut list would reach Hermes as broken JSON.
MAX_WAITING_KEPT_ANSWER_CHARS = MAX_WAITING_ANSWER_CHARS + MAX_WAITING_CHOICES * (2 * MAX_WAITING_CHOICE_CHARS + 4)
APPROVAL_CHOICES = ("once", "deny")
RECOMMENDED_LABEL = "(Recommended)"
# Words a spoken answer is read with, as Conduit reads them
# (HermesCallWaiting.pickedChoices): the two move together.
_NEGATIONS = frozenset({"not", "no", "don", "dont", "never", "without", "except", "neither", "nor", "instead"})
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
    "ninth": 9, "tenth": 10,
    "1st": 1, "2nd": 2, "3rd": 3, "4th": 4, "5th": 5, "6th": 6, "7th": 7, "8th": 8, "9th": 9, "10th": 10,
}
# Words that may stand around choice numbers ("option 2, please").
_NUMBER_FILLER = frozenset({"the", "a", "an", "and", "or", "option", "options", "choice", "choices", "number",
                            "numbers", "please", "pick", "choose", "go", "with", "take", "i", "ll", "d", "want",
                            "that", "this", "both"})
# The words after which "one" is still a number ("option one").
_ONE_LEADS = frozenset({"option", "choice", "number", "and", "or", "with", "take", "pick", "choose"})
# A spoken request for Hermes to say more before the user chooses: sent as
# the answer, so Hermes explains and asks again.
ASKS_FIRST_LEAD = "Before I choose:"

# A Hermes session key, kept whole: one with a control character or over this
# many UTF-8 bytes is dropped, never cut. The same rule as the plugin's
# events.session_key; this module imports nothing from the plugin package.
SESSION_KEY_MAX_BYTES = 512
_SESSION_KEY_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# The shape the call event carries (events.sanitize_call).
_WATCH_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}\Z")
_thread_lock = threading.Lock()


class CallStore:
    def __init__(self, home: Path, clock: Callable[[], float] = time.time,
                 place_blocked: Callable[[dict[str, Any]], bool] | None = None) -> None:
        self.path = Path(home) / FILE_NAME
        self.clock = clock
        # Whether the user is at a place they don't want calls at, given the
        # settings (calls.py asks the location store; the dashboard never
        # decides calls, so it passes nothing).
        self.place_blocked = place_blocked

    # --- Settings ---------------------------------------------------------

    def settings(self) -> dict[str, Any]:
        return dict(self._load()["settings"])

    def update_settings(self, changes: Any) -> dict[str, Any]:
        if not isinstance(changes, dict):
            raise ValueError("settings must be an object")
        kept = dict(changes)
        for key, value in changes.items():
            if key in BOOL_SETTINGS:
                if not isinstance(value, bool):
                    raise ValueError(f"{key} must be true or false")
            elif key in INT_BOUNDS:
                low, high = INT_BOUNDS[key]
                if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
                    raise ValueError(f"{key} must be a whole number from {low} to {high}")
            elif key == "rules":
                rules = _rules(value)
                if rules is None:
                    raise ValueError(f"rules must be text of up to {MAX_RULES_CHARS} characters")
                kept[key] = rules
            elif key == "avoid_places":
                places = _place_ids(value)
                if places is None:
                    raise ValueError(f"avoid_places must be a list of up to {MAX_AVOID_PLACES} place ids")
                kept[key] = places
            else:
                raise ValueError(f"unknown setting {key}")
        with self._locked() as state:
            state["settings"].update(kept)
            return dict(state["settings"])

    # --- Watches ------------------------------------------------------------

    def add_watch(self, session_ids: Any, title: Any, hold_s: Any = 0, ended_within_s: Any = None) -> dict[str, Any]:
        """Watches a job, held for ``hold_s`` seconds while the user's call
        goes on. ``{"status": "watching", "id"}``, or ``{"status": "ended",
        "outcome"}`` when one of the job's sessions ended within the last
        ``ended_within_s`` seconds (how long ago the job's request went out,
        on the phone's clock, so an earlier turn of the same chat never
        counts): no watch is kept and the caller tells the user itself. An
        interrupted turn doesn't count: during a call that is the call putting
        a correction into the job, which goes on in a new turn. Raises ValueError("calls_off") unless calls and "call when I ask" are
        on."""
        ids = _session_ids(session_ids)
        clean_title = _title(title)
        hold = _seconds(hold_s, "hold_s", MAX_HOLD_S)
        within = None if ended_within_s is None else _seconds(ended_within_s, "ended_within_s", RECENT_END_TTL_S)
        with self._locked() as state:
            settings = state["settings"]
            if not settings["enabled"] or not settings["when_asked"]:
                raise ValueError("calls_off")
            now = self.clock()
            if within is not None:
                for end in state["ends"]:
                    if end["session_id"] in ids and end["outcome"] != "stopped" and now - end["at"] <= within:
                        return {"status": "ended", "outcome": end["outcome"]}
            # A retried request (the first answer lost) gets the same watch,
            # never a second one that would call again.
            for watch in state["watches"]:
                if watch["origin"] != "alert" and set(watch["session_ids"]).issubset(ids):
                    watch["session_ids"] = ids
                    # A watch Hermes set on its own becomes the user's: their
                    # request calls even with "Hermes decides" off.
                    watch["origin"] = "conduit"
                    watch["asked"] = True
                    if not watch["title"]:
                        watch["title"] = clean_title
                    if hold:
                        watch["hold_until"] = max(watch["hold_until"], now + hold)
                    return {"status": "watching", "id": watch["id"]}
            if len(state["watches"]) >= MAX_WATCHES:
                raise ValueError("too_many")
            watch = _new_watch(ids, now, title=clean_title, hold_until=now + hold if hold else 0.0)
            state["watches"].append(watch)
            return {"status": "watching", "id": watch["id"]}

    def add_tool_watch(self, session_id: Any, reason: Any, asked: bool) -> dict[str, Any]:
        """Hermes asked to call the user when this turn of ``session_id``
        ends (``asked``: because the user asked it to). ``{"status":
        "watching", "id"}``; a session already watched keeps its watch,
        taking the reason if it had none. Raises ValueError("calls_off") when
        calls, or this kind of call, are off, or ("too_many")."""
        ids = _session_ids([session_id])
        clean_reason = _reason(reason)
        with self._locked() as state:
            settings = state["settings"]
            if not settings["enabled"] or not settings["when_asked" if asked else "decides"]:
                raise ValueError("calls_off")
            now = self.clock()
            for watch in state["watches"]:
                if watch["origin"] != "alert" and ids[0] in watch["session_ids"]:
                    if not watch["reason"]:
                        watch["reason"] = clean_reason
                    watch["asked"] = watch["asked"] or asked
                    return {"status": "watching", "id": watch["id"]}
            if len(state["watches"]) >= MAX_WATCHES:
                raise ValueError("too_many")
            watch = _new_watch(ids, now, origin="tool", reason=clean_reason, asked=asked)
            state["watches"].append(watch)
            return {"status": "watching", "id": watch["id"]}

    def add_alert(self, session_id: str | list[str], kind: str, reason: Any,
                  delay_s: int = ALERT_DELAY_S) -> str | None:
        """Hermes is waiting on the user's approval or answer in
        ``session_id`` (or a list of the ids one session goes by, the first
        the one the call opens): unless it's answered within ``delay_s``
        seconds (``cancel_alerts``), the waiter calls. The alert's id, or None
        when alerts are off (nothing is written then) or one is already
        waiting. An id that isn't a session key is left out, so the others
        still call."""
        if kind not in ALERT_KINDS:
            raise ValueError(f"unknown alert {kind}")
        named = session_id if isinstance(session_id, list) else [session_id]
        ids = _session_ids([value for value in named if isinstance(value, str) and _session_key(value.strip())])
        delay = _seconds(delay_s, "delay_s", MAX_HOLD_S)
        settings = self.settings()
        if not settings["enabled"] or not settings["alerts"]:
            return None
        with self._locked() as state:
            if not state["settings"]["enabled"] or not state["settings"]["alerts"]:
                return None
            # The same wait, whichever of its ids a replayed hook names.
            if any(watch["origin"] == "alert" and set(watch["session_ids"]) & set(ids)
                   and watch["pending"]["outcome"] == kind for watch in state["watches"]):
                return None
            if len(state["watches"]) >= MAX_WATCHES:
                return None
            now = self.clock()
            watch = _new_watch(ids, now, origin="alert", reason=_reason(reason), hold_until=now + delay)
            watch["pending"] = {"outcome": kind, "session_id": ids[0]}
            state["watches"].append(watch)
            return watch["id"]

    def cancel_alerts(self, session_id: Any, kind: str | None = None) -> int:
        """The user answered (or the turn moved on): ``session_id``'s waiting
        alerts, of ``kind`` or all, call no more. How many were removed."""
        if not isinstance(session_id, str) or not session_id.strip() or not self.path.exists():
            return 0
        session_id = session_id.strip()

        def cancelled(watch: dict[str, Any]) -> bool:
            return (watch["origin"] == "alert" and session_id in watch["session_ids"]
                    and (kind is None or watch["pending"]["outcome"] == kind))

        if not any(cancelled(watch) for watch in self._load()["watches"]):
            return 0
        with self._locked() as state:
            kept = [watch for watch in state["watches"] if not cancelled(watch)]
            removed = len(state["watches"]) - len(kept)
            state["watches"] = kept
            return removed

    def alert_now(self, session_id: Any, kind: str, reason: Any, *, seed: str = "") -> dict[str, Any] | None:
        """A failed turn with alerts on: the result ``fire`` would give for a
        watch on it (``call``, ``limited``, ``busy``), counted against the
        limits. None when alerts are off: nothing is written then. ``seed``
        (the turn) fixes the call's id, so a replayed hook is the same call
        to the relay, which rings it once, and gets the first hook's
        decision without counting again."""
        ids = _session_ids([session_id])
        settings = self.settings()
        if not settings["enabled"] or not settings["alerts"]:
            return None
        with self._locked() as state:
            now = self.clock()
            watch = _new_watch(ids, now, origin="alert", reason=_reason(reason))
            if not seed:
                return _decide(state, watch, kind, now, self.place_blocked)
            watch["id"] = hashlib.sha256("\0".join([kind, *ids, seed]).encode()).hexdigest()[:24]
            earlier = next((entry for entry in state["seeded"] if entry["id"] == watch["id"]), None)
            if earlier is not None:
                # Not held back by the gap the first call opened: that would
                # send the failure notification beside its ring.
                return {"watch": watch, "outcome": kind, "status": earlier["status"]}
            result = _decide(state, watch, kind, now, self.place_blocked)
            state["seeded"] = [*state["seeded"], {"id": watch["id"], "status": result["status"], "at": now}][-MAX_SEEDED:]
            return result

    # --- What Hermes waits on -----------------------------------------------

    def note_waiting(self, session_id: str | list[str], kind: str, *, request_id: str = "",
                     questions: Any = None, description: Any = "", command: Any = "") -> bool:
        """Hermes waits on the user in ``session_id`` (or the ids its session
        goes by): a question batch (``request_id``, the clarify loop's own
        decision id, and ``questions``) or an approval (``description`` and
        ``command``). Kept so a call about it can read and answer it with no
        chat open. Only with alert calls on (False otherwise: nothing is
        written). A newer wait of the same kind in the session replaces the
        older one."""
        if kind not in ALERT_KINDS:
            raise ValueError(f"unknown wait {kind}")
        named = session_id if isinstance(session_id, list) else [session_id]
        try:
            ids = _session_ids([value for value in named if isinstance(value, str) and _session_key(value.strip())])
        except ValueError:
            return False
        settings = self.settings()
        if not settings["enabled"] or not settings["alerts"]:
            return False
        entry = _waiting_entry({
            "id": secrets.token_hex(12), "session_ids": ids, "kind": kind, "at": self.clock(),
            "request_id": request_id, "questions": questions, "description": description,
            "command": command if isinstance(command, str) else "",
            "command_sha": hashlib.sha256(command.encode("utf-8")).hexdigest() if isinstance(command, str) and command else "",
            "cut": isinstance(command, str) and len(command) > MAX_WAITING_COMMAND_CHARS,
        }, self.clock())
        if entry is None:
            return False
        with self._locked() as state:
            if not state["settings"]["enabled"] or not state["settings"]["alerts"]:
                return False
            kept = [item for item in state["waiting"]
                    if not (item["kind"] == kind and set(item["session_ids"]) & set(ids))]
            state["waiting"] = [*kept, entry][-MAX_WAITING:]
            return True

    def forget_waiting(self, session_id: Any, kind: str | None = None) -> int:
        """The wait ended (answered anywhere, or the turn moved on): how many
        were dropped."""
        if not isinstance(session_id, str) or not session_id.strip() or not self.path.exists():
            return 0
        session_id = session_id.strip()

        def ended(item: dict[str, Any]) -> bool:
            return session_id in item["session_ids"] and (kind is None or item["kind"] == kind)

        if not any(ended(item) for item in self._load()["waiting"]):
            return 0
        with self._locked() as state:
            kept = [item for item in state["waiting"] if not ended(item)]
            removed = len(state["waiting"]) - len(kept)
            state["waiting"] = kept
            return removed

    def waiting_for(self, session_id: Any) -> dict[str, Any] | None:
        """What Hermes waits on in ``session_id``, as a call reads it: the
        newest wait, with its questions numbered and the ones answered from a
        call marked. None when nothing waits."""
        if not isinstance(session_id, str) or not session_id.strip() or not self.path.exists():
            return None
        session_id = session_id.strip()
        found = [item for item in self._load()["waiting"] if session_id in item["session_ids"]]
        return _public_waiting(max(found, key=lambda item: item["at"])) if found else None

    def answer_waiting(self, session_id: Any, *, answer: Any = None, choice: Any = None,
                       question: Any = None, reason: Any = None) -> dict[str, Any]:
        """The user's spoken answer, from a call, to what Hermes waits on in
        ``session_id``. A question takes ``answer`` (a choice's number or
        label, "the recommended one", or their own words) for ``question``
        (its number in the batch; the first unanswered one by default). An
        approval takes ``choice`` (once or deny) and, for a deny, ``reason``.
        The agent's process passes it on (``take_answers``,
        ``take_approval``): ``{"status": "answered", ...}`` says it's on
        its way, with the batch's next question to ask, if any."""
        if not isinstance(session_id, str) or not _session_key(session_id.strip()):
            raise ValueError("session_id must be a session id")
        session_id = session_id.strip()
        with self._locked() as state:
            found = [item for item in state["waiting"] if session_id in item["session_ids"]]
            if not found:
                return {"status": "not_waiting", "message": "Hermes isn't waiting on the user in that chat any more."}
            item = max(found, key=lambda entry: entry["at"])
            if item["kind"] == "approval":
                picked = choice.strip().lower() if isinstance(choice, str) else ""
                if picked not in APPROVAL_CHOICES:
                    raise ValueError("choice must be once or deny")
                if picked == "once" and item["cut"]:
                    return {"status": "refused",
                            "message": "The command is too long to read out whole, so it can only be denied here. "
                                       "The user can approve it in Conduit."}
                if picked == "once" and not item["command"]:
                    # Never approve what wasn't shown (as the Watch's job cards).
                    return {"status": "refused",
                            "message": "Hermes didn't say which command it wants to run, so it can only be denied "
                                       "here. The user can approve it in Conduit."}
                if item["choice"] is not None:
                    return {"status": "answered", "choice": item["choice"]}
                item["choice"] = picked
                item["reason"] = _waiting_text(reason, MAX_WAITING_ANSWER_CHARS) if picked == "deny" else ""
                return {"status": "answered", "choice": picked}
            if not isinstance(answer, str) or not answer.strip():
                raise ValueError("answer must be the user's answer")
            open_numbers = [index for index, entry in enumerate(item["questions"]) if entry["answer"] is None]
            if not open_numbers:
                return {"status": "answered", "message": "Every question is answered already."}
            if question is None:
                index = open_numbers[0]
            elif isinstance(question, int) and not isinstance(question, bool) and 1 <= question <= len(item["questions"]):
                index = question - 1
            else:
                raise ValueError(f"question must be a number from 1 to {len(item['questions'])}")
            entry = item["questions"][index]
            if entry["answer"] is not None:
                return {"status": "answered", "message": f"Question {index + 1} is answered already."}
            value, labels = map_answer(answer[:MAX_WAITING_ANSWER_CHARS], entry["choices"], entry["multi_select"])
            entry["answer"] = value
            result: dict[str, Any] = {"status": "answered", "question": index + 1,
                                      "answer": ", ".join(labels) if labels else value}
            following = next((number for number in open_numbers if number != index), None)
            if following is not None:
                result["next"] = _public_question(item["questions"][following], following)
            return result

    def answers_ready(self, request_id: Any) -> bool:
        """Whether a call has answered every question of the clarify loop's
        batch ``request_id``. Reads only."""
        if not isinstance(request_id, str) or not request_id or not self.path.exists():
            return False
        return any(_complete(item, request_id) for item in self._load()["waiting"])

    def take_answers(self, request_id: Any) -> dict[str, str] | None:
        """The clarify loop's question batch ``request_id``, once every
        question has an answer from a call: ``{qid: answer}``, taken."""
        if not self.answers_ready(request_id):
            return None

        def complete(item: dict[str, Any]) -> bool:
            return _complete(item, request_id)

        with self._locked() as state:
            item = next((entry for entry in state["waiting"] if complete(entry)), None)
            if item is None:
                return None
            state["waiting"] = [entry for entry in state["waiting"] if entry is not item]
            return {entry["qid"]: entry["answer"] for entry in item["questions"]}

    def take_approval(self, session_id: Any, *, keep: bool = False) -> dict[str, Any] | None:
        """An approval in ``session_id`` answered from a call: its
        ``choice``, ``reason`` and ``command_sha``, taken (only read with
        ``keep``). None while unanswered or gone."""
        if not isinstance(session_id, str) or not session_id.strip() or not self.path.exists():
            return None
        session_id = session_id.strip()

        def answered(item: dict[str, Any]) -> bool:
            return item["kind"] == "approval" and session_id in item["session_ids"] and item["choice"] is not None

        if not any(answered(item) for item in self._load()["waiting"]):
            return None
        with self._locked() as state:
            item = next((entry for entry in state["waiting"] if answered(entry)), None)
            if item is None:
                return None
            if not keep:
                state["waiting"] = [entry for entry in state["waiting"] if entry is not item]
            return {"choice": item["choice"], "reason": item["reason"], "command_sha": item["command_sha"]}

    def has_waiting(self, session_id: Any, kind: str) -> bool:
        if not isinstance(session_id, str) or not session_id.strip() or not self.path.exists():
            return False
        return any(item["kind"] == kind and session_id.strip() in item["session_ids"]
                   for item in self._load()["waiting"])

    # --- Presence -----------------------------------------------------------

    def set_presence(self, hold_s: Any) -> dict[str, Any]:
        """The user is in a Live Voice call for ``hold_s`` more seconds
        (Conduit renews it during the call), or not (0, at hang-up): nothing
        rings meanwhile."""
        hold = _seconds(hold_s, "hold_s", MAX_HOLD_S)
        if not hold and not self.path.exists():
            return {"status": "away"}
        with self._locked() as state:
            state["presence_until"] = self.clock() + hold if hold else 0.0
            return {"status": "present" if hold else "away"}

    def hold(self, watch_id: Any, hold_s: Any) -> dict[str, Any]:
        """Renews a watch's hold for ``hold_s`` seconds while the call goes
        on, or releases it with 0 at hang-up. Releasing a watch whose job
        ended during the hold answers ``{"status": "ended", "outcome"}`` and
        consumes it: the caller tells the user itself. Otherwise
        ``{"status": "watching"}``, or ``{"status": "gone"}`` for a watch that
        no longer exists (fired, removed or expired)."""
        hold = _seconds(hold_s, "hold_s", MAX_HOLD_S)
        if not isinstance(watch_id, str) or not watch_id or not self.path.exists():
            return {"status": "gone"}
        with self._locked() as state:
            now = self.clock()
            index = next((i for i, watch in enumerate(state["watches"]) if watch["id"] == watch_id), None)
            if index is None:
                return {"status": "gone"}
            watch = state["watches"][index]
            if hold:
                watch["hold_until"] = now + hold
                return {"status": "watching"}
            if watch["pending"] is not None:
                state["watches"].pop(index)
                return {"status": "ended", "outcome": watch["pending"]["outcome"]}
            watch["hold_until"] = 0.0
            return {"status": "watching"}

    def remove_watch(self, watch_id: Any) -> bool:
        if not isinstance(watch_id, str) or not watch_id:
            return False
        if not self.path.exists():
            return False
        with self._locked() as state:
            kept = [watch for watch in state["watches"] if watch["id"] != watch_id]
            removed = len(kept) != len(state["watches"])
            state["watches"] = kept
            return removed

    # --- Outcomes -----------------------------------------------------------

    def record_outcome(self, call_id: Any, session_ids: Any, outcome: Any, *, kind: Any = None, title: Any = "",
                       reason: Any = "", age_s: Any = 0) -> dict[str, Any]:
        """The user declined call ``call_id`` or didn't answer it, ``age_s``
        seconds ago (on the phone's clock, so the host's never matters):
        kept until the next turn of one of ``session_ids`` takes it. A
        retried report keeps the first. ``{"status": "recorded"}``; raises
        ValueError for anything malformed."""
        if not isinstance(call_id, str) or not _WATCH_ID.fullmatch(call_id):
            raise ValueError("call_id must be the call's id")
        ids = _session_ids(session_ids)
        if outcome not in CALL_OUTCOMES:
            raise ValueError("outcome must be declined or missed")
        # Below the TTL: one exactly a day old would be dropped unheard.
        age = _seconds(age_s, "age_s", OUTCOME_TTL_S - 1)
        with self._locked() as state:
            known = {entry["id"] for entry in state["outcomes"]} | {entry["id"] for entry in state["told"]}
            if call_id not in known:
                state["outcomes"] = [*state["outcomes"], {
                    "id": call_id,
                    "session_ids": ids,
                    "outcome": outcome,
                    # A kind a newer Conduit knows reads as none.
                    "kind": kind if kind in CALL_KINDS else None,
                    "title": _title(title),
                    "reason": _reason(reason),
                    "at": self.clock() - age,
                }][-MAX_OUTCOMES:]
            return {"status": "recorded"}

    def take_outcomes(self, session_id: Any) -> list[dict[str, Any]]:
        """The outcomes waiting for a turn of ``session_id``, oldest first:
        taken, so the next turn doesn't hear them again, nor a late retry of
        their report."""
        if not isinstance(session_id, str) or not session_id.strip() or not self.path.exists():
            return []
        session_id = session_id.strip()
        # Every turn asks: only one with something to take writes.
        if not any(session_id in entry["session_ids"] for entry in self._load()["outcomes"]):
            return []
        with self._locked() as state:
            taken = sorted((entry for entry in state["outcomes"] if session_id in entry["session_ids"]),
                           key=lambda entry: entry["at"])
            state["outcomes"] = [entry for entry in state["outcomes"] if session_id not in entry["session_ids"]]
            now = self.clock()
            state["told"] = [*state["told"], *({"id": entry["id"], "at": now} for entry in taken)][-MAX_TOLD:]
            return taken

    def watch_count(self) -> int:
        now = self.clock()
        return sum(1 for watch in self._load()["watches"] if watch["expires_at"] > now)

    # --- Firing -------------------------------------------------------------

    def fire(self, session_id: Any, outcome: str) -> dict[str, Any] | None:
        """A turn of ``session_id`` ended with ``outcome``.

        None when no watch covers the session (its waiting alerts are
        dropped: the turn no longer waits on the user). A held watch keeps the first
        outcome and answers ``held`` (with ``until``): the call waits for the
        hold to end. An interrupted turn isn't kept while held: the call
        interrupts a job to put a correction into it, and the job goes on in
        a new turn (a job the user stops in the call has its watch removed).
        Otherwise the watch is consumed and the result says what
        to do: ``call`` (counted against the limits), ``limited`` (with
        ``reason`` gap, hour or day), ``busy`` (the user is in a Live Voice
        call) or ``off`` (calls, or this kind of call, were turned off after
        the watch was registered).
        """
        if outcome not in OUTCOMES:
            raise ValueError(f"unknown outcome {outcome}")
        if not isinstance(session_id, str) or not session_id.strip() or not self.path.exists():
            return None
        session_id = session_id.strip()
        with self._locked() as state:
            now = self.clock()
            settings = state["settings"]
            if settings["enabled"]:
                ends = [end for end in state["ends"] if end["session_id"] != session_id]
                ends.append({"session_id": session_id, "outcome": outcome, "at": now})
                state["ends"] = ends[-MAX_RECENT_ENDS:]
            # The turn moved on: nothing it waited on calls any more.
            state["watches"] = [watch for watch in state["watches"]
                                if not (watch["origin"] == "alert" and session_id in watch["session_ids"])]
            index = next((i for i, watch in enumerate(state["watches"]) if session_id in watch["session_ids"]), None)
            if index is None:
                return None
            watch = state["watches"][index]
            if watch["hold_until"] > now:
                if watch["pending"] is None and outcome != "stopped":
                    watch["pending"] = {"outcome": outcome, "session_id": session_id}
                pending = watch["pending"]
                return {"watch": dict(watch), "outcome": pending["outcome"] if pending else None, "status": "held",
                        "until": watch["hold_until"]}
            state["watches"].pop(index)
            return _decide(state, watch, outcome, now, self.place_blocked)

    def fire_due(self) -> list[dict[str, Any]]:
        """Watches whose job ended while held and whose hold has run out
        (the call's phone went away without releasing them), and alerts left
        unanswered: consumed, each with the result ``fire`` would give, plus
        the ``session_id`` that ended."""
        if not self.path.exists():
            return []
        with self._locked() as state:
            now = self.clock()
            due = [watch for watch in state["watches"] if watch["pending"] is not None and watch["hold_until"] <= now]
            if not due:
                return []
            due_ids = {watch["id"] for watch in due}
            state["watches"] = [watch for watch in state["watches"] if watch["id"] not in due_ids]
            return [{**_decide(state, watch, watch["pending"]["outcome"], now, self.place_blocked),
                     "session_id": watch["pending"]["session_id"]} for watch in due]

    def next_due(self) -> float | None:
        """When the earliest hold over an ended job runs out, if any."""
        holds = [watch["hold_until"] for watch in self._load()["watches"] if watch["pending"] is not None]
        return min(holds) if holds else None

    # --- Storage ------------------------------------------------------------

    def _load(self, *, for_write: bool = False) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, RecursionError):
            raw = None
        except OSError:
            # Unreadable for now (not missing, not corrupt): a write would
            # replace the watches it couldn't read, so it fails instead.
            if for_write:
                raise
            raw = None
        return _normalized(raw, self.clock())

    @contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        """Load, let the caller change, then save: all under the file lock."""
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self.path.with_name(f".{self.path.name}.lock")
        with _thread_lock, open(lock_path, "a+b") as handle, _exclusive(handle):
            state = self._load(for_write=True)
            before = _encoded(state)
            yield state
            # Most turn ends change nothing: no write then.
            after = _encoded(state)
            if after != before or not self.path.exists():
                self._save(after)

    def _save(self, encoded: str) -> None:
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        # Private from the first byte: it holds job titles and session ids.
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(encoded)
        temporary.chmod(0o600)
        temporary.replace(self.path)
        self.path.chmod(0o600)


def _encoded(state: dict[str, Any]) -> str:
    return json.dumps(state, separators=(",", ":")) + "\n"


def _new_watch(ids: list[str], now: float, *, title: str = "", origin: str = "conduit", reason: str = "",
               asked: bool = False, hold_until: float = 0.0) -> dict[str, Any]:
    return {
        "id": secrets.token_hex(12),
        "session_ids": ids,
        "title": title,
        "created_at": now,
        "expires_at": now + WATCH_TTL_S,
        "hold_until": hold_until,
        "pending": None,
        "origin": origin,
        "reason": reason,
        "asked": asked,
    }


def _allowed(settings: dict[str, Any], watch: dict[str, Any]) -> bool:
    """Whether the user's settings still allow this kind of call."""
    if not settings["enabled"]:
        return False
    if watch["origin"] == "alert":
        return settings["alerts"]
    if watch["origin"] == "tool":
        return settings["when_asked"] if watch["asked"] else settings["decides"]
    return True


def _decide(state: dict[str, Any], watch: dict[str, Any], outcome: str, now: float,
            place_blocked: Callable[[dict[str, Any]], bool] | None = None) -> dict[str, Any]:
    """Whether a consumed watch calls, within the user's settings and limits.
    A call counts against them once decided, even if its delivery later fails
    (the usual push goes out then): erring towards fewer calls, never more."""
    settings = state["settings"]
    result: dict[str, Any] = {"watch": watch, "outcome": outcome}
    if not _allowed(settings, watch):
        return {**result, "status": "off"}
    if state["presence_until"] > now or (watch["origin"] != "conduit" and _in_call(state, now)):
        # The user is talking to Hermes right now: no ringing over it.
        return {**result, "status": "busy"}
    if place_blocked is not None and (settings["avoid_places"] or settings["avoid_unknown_place"]):
        try:
            blocked = place_blocked(settings)
        except Exception:  # noqa: BLE001 — a broken location store never stops a call
            blocked = False
        if blocked:
            # Not counted against the limits: nothing rang.
            return {**result, "status": "limited", "reason": "place"}
    history = state["history"]
    if history and now - max(history) < settings["min_gap_s"]:
        return {**result, "status": "limited", "reason": "gap"}
    if sum(1 for at in history if now - at < HOUR_S) >= settings["per_hour"]:
        return {**result, "status": "limited", "reason": "hour"}
    if sum(1 for at in history if now - at < DAY_S) >= settings["per_day"]:
        return {**result, "status": "limited", "reason": "day"}
    history.append(now)
    return {**result, "status": "call"}


def _in_call(state: dict[str, Any], now: float) -> bool:
    """Conduit holds a watch through the call it was asked for in (an app
    without presence too). Not for a held watch's own call: its siblings in
    that call run out moments apart once the phone has gone away. A call
    with no held watch (one a ringing call opened) is covered by presence
    alone."""
    return any(watch["origin"] == "conduit" and watch["hold_until"] > now for watch in state["watches"])


def _normalized(raw: Any, now: float) -> dict[str, Any]:
    """A well-formed state from whatever is on disk, with expired entries
    dropped. Anything malformed reads as its default."""
    raw = raw if isinstance(raw, dict) else {}
    settings = dict(DEFAULT_SETTINGS)
    stored = raw.get("settings") if isinstance(raw.get("settings"), dict) else {}
    for key in BOOL_SETTINGS:
        if isinstance(stored.get(key), bool):
            settings[key] = stored[key]
    for key, (low, high) in INT_BOUNDS.items():
        value = stored.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and low <= value <= high:
            settings[key] = value
    settings["rules"] = _rules(stored.get("rules")) or ""
    settings["avoid_places"] = _place_ids(stored.get("avoid_places")) or []
    watches = []
    for watch in raw.get("watches") if isinstance(raw.get("watches"), list) else []:
        try:
            if watch["expires_at"] > now and _WATCH_ID.match(str(watch["id"])):
                origin = watch.get("origin") if watch.get("origin") in ORIGINS else "conduit"
                pending = watch.get("pending")
                kinds = ALERT_KINDS if origin == "alert" else OUTCOMES
                if not (isinstance(pending, dict) and pending.get("outcome") in kinds
                        and isinstance(pending.get("session_id"), str)):
                    pending = None
                if origin == "alert" and pending is None:
                    continue
                hold_until = watch.get("hold_until", 0.0)
                watches.append({
                    "id": str(watch["id"]),
                    "session_ids": _session_ids(watch["session_ids"]),
                    "title": _title(watch.get("title")),
                    "created_at": float(watch["created_at"]),
                    "expires_at": float(watch["expires_at"]),
                    "hold_until": float(hold_until) if isinstance(hold_until, (int, float)) and not isinstance(hold_until, bool) else 0.0,
                    "pending": {"outcome": pending["outcome"], "session_id": pending["session_id"]} if pending else None,
                    "origin": origin,
                    "reason": _reason(watch.get("reason")),
                    "asked": watch.get("asked") is True,
                })
        except (KeyError, TypeError, ValueError):
            continue
    stored_history = raw.get("history") if isinstance(raw.get("history"), list) else []
    history = sorted(float(at) for at in stored_history
                     if isinstance(at, (int, float)) and not isinstance(at, bool) and now - at < DAY_S)
    history = history[-MAX_HISTORY:]
    ends = []
    for end in raw.get("ends") if isinstance(raw.get("ends"), list) else []:
        if (isinstance(end, dict) and isinstance(end.get("session_id"), str) and end.get("outcome") in OUTCOMES
                and isinstance(end.get("at"), (int, float)) and now - end["at"] < RECENT_END_TTL_S):
            ends.append({"session_id": end["session_id"], "outcome": end["outcome"], "at": float(end["at"])})
    seeded = []
    for entry in raw.get("seeded") if isinstance(raw.get("seeded"), list) else []:
        if (isinstance(entry, dict) and isinstance(entry.get("id"), str) and _WATCH_ID.match(entry["id"])
                and isinstance(entry.get("status"), str) and isinstance(entry.get("at"), (int, float))
                and not isinstance(entry.get("at"), bool) and now - entry["at"] < DAY_S):
            seeded.append({"id": entry["id"], "status": entry["status"], "at": float(entry["at"])})
    outcomes = []
    for entry in raw.get("outcomes") if isinstance(raw.get("outcomes"), list) else []:
        try:
            at = entry["at"]
            if (isinstance(at, (int, float)) and not isinstance(at, bool) and now - at < OUTCOME_TTL_S
                    and _WATCH_ID.match(str(entry["id"])) and entry["outcome"] in CALL_OUTCOMES):
                outcomes.append({
                    "id": str(entry["id"]),
                    "session_ids": _session_ids(entry["session_ids"]),
                    "outcome": entry["outcome"],
                    "kind": entry.get("kind") if entry.get("kind") in CALL_KINDS else None,
                    "title": _title(entry.get("title")),
                    "reason": _reason(entry.get("reason")),
                    "at": float(at),
                })
        except (KeyError, TypeError, ValueError):
            continue
    told = []
    for entry in raw.get("told") if isinstance(raw.get("told"), list) else []:
        if (isinstance(entry, dict) and isinstance(entry.get("id"), str) and _WATCH_ID.fullmatch(entry["id"])
                and isinstance(entry.get("at"), (int, float)) and not isinstance(entry.get("at"), bool)
                and now - entry["at"] < OUTCOME_TTL_S):
            told.append({"id": entry["id"], "at": float(entry["at"])})
    presence = raw.get("presence_until", 0.0)
    presence_until = float(presence) if isinstance(presence, (int, float)) and not isinstance(presence, bool) and presence > now else 0.0
    waiting = []
    for entry in raw.get("waiting") if isinstance(raw.get("waiting"), list) else []:
        kept = _waiting_entry(entry, now) if isinstance(entry, dict) else None
        if kept is not None:
            waiting.append(kept)
    return {"v": VERSION, "settings": settings, "watches": watches, "history": history,
            "ends": ends[-MAX_RECENT_ENDS:], "seeded": seeded[-MAX_SEEDED:], "presence_until": presence_until,
            "outcomes": outcomes[-MAX_OUTCOMES:], "told": told[-MAX_TOLD:], "waiting": waiting[-MAX_WAITING:]}


def _waiting_text(value: Any, limit: int) -> str:
    text = value.strip() if isinstance(value, str) else ""
    text = "".join(char for char in text if char.isprintable() or char == "\n")
    return text[:limit]


def _waiting_entry(raw: dict[str, Any], now: float) -> dict[str, Any] | None:
    """A well-formed wait from ``raw``, or None when it is malformed or
    older than WAITING_TTL_S."""
    try:
        at = raw["at"]
        if not isinstance(at, (int, float)) or isinstance(at, bool) or now - at >= WAITING_TTL_S:
            return None
        kind = raw["kind"]
        if kind not in ALERT_KINDS or not _WATCH_ID.match(str(raw["id"])):
            return None
        entry: dict[str, Any] = {"id": str(raw["id"]), "session_ids": _session_ids(raw["session_ids"]),
                                 "kind": kind, "at": float(at)}
    except (KeyError, TypeError, ValueError):
        return None
    if kind == "approval":
        description = _waiting_text(raw.get("description"), MAX_WAITING_TEXT_CHARS)
        if not description:
            return None
        choice = raw.get("choice")
        sha = raw.get("command_sha")
        entry.update({
            "description": description,
            "command": _waiting_text(raw.get("command"), MAX_WAITING_COMMAND_CHARS),
            "command_sha": sha if isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha) else "",
            "cut": raw.get("cut") is True,
            "choice": choice if choice in APPROVAL_CHOICES else None,
            "reason": _waiting_text(raw.get("reason"), MAX_WAITING_ANSWER_CHARS),
        })
        return entry
    request_id = raw.get("request_id")
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", request_id):
        return None
    questions = []
    for item in raw.get("questions") if isinstance(raw.get("questions"), list) else []:
        if not isinstance(item, dict) or len(questions) >= MAX_WAITING_QUESTIONS:
            continue
        qid = item.get("qid")
        text = _waiting_text(item.get("question"), MAX_WAITING_TEXT_CHARS)
        if not isinstance(qid, str) or not qid or len(qid) > 128 or not text:
            continue
        choices = [_waiting_text(choice, MAX_WAITING_CHOICE_CHARS) for choice in item.get("choices") or []
                   if isinstance(choice, str) and choice.strip()][:MAX_WAITING_CHOICES]
        answer = item.get("answer")
        questions.append({
            "qid": qid, "question": text, "choices": choices, "multi_select": item.get("multi_select") is True,
            "answer": answer[:MAX_WAITING_KEPT_ANSWER_CHARS] if isinstance(answer, str) else None,
        })
    if not questions:
        return None
    entry.update({"request_id": request_id, "questions": questions})
    return entry


def _complete(item: dict[str, Any], request_id: str) -> bool:
    return (item["kind"] == "question" and item["request_id"] == request_id and bool(item["questions"])
            and all(entry["answer"] is not None for entry in item["questions"]))


def _public_question(entry: dict[str, Any], index: int) -> dict[str, Any]:
    return {"number": index + 1, "question": entry["question"], "choices": list(entry["choices"]),
            "multi_select": entry["multi_select"], "answered": entry["answer"] is not None}


def _public_waiting(item: dict[str, Any]) -> dict[str, Any]:
    if item["kind"] == "approval":
        return {"kind": "approval", "description": item["description"], "command": item["command"],
                "command_cut": item["cut"], "answered": item["choice"] is not None}
    return {"kind": "question",
            "questions": [_public_question(entry, index) for index, entry in enumerate(item["questions"])]}


def map_answer(answer: str, choices: list[str], multi_select: bool) -> tuple[str, list[str]]:
    """The value a spoken answer sends for a question with ``choices``, and
    the labels it picked: a choice's number or ordinal ("two", "the second
    one", "1 and 3" for several), its label as written, or "the recommended
    one" pick choices, as the card's tap would. Anything else, and an answer
    that asks Hermes to say more first (``ASKS_FIRST_LEAD``), goes as the
    user's own words. Conduit's own mapping is HermesCallWaiting.answerValue
    (Conduit/Voice/HermesCallWaiting.swift): the two move together."""
    text = " ".join(answer.split())
    if not choices or text.casefold().startswith(ASKS_FIRST_LEAD.rstrip(":").casefold()):
        return text, []
    picked = _picked_choices(text, choices, multi_select)
    if not picked:
        return text, []
    labels = [_without_recommended(choices[index]) for index in picked]
    if multi_select:
        return json.dumps([choices[index] for index in picked], ensure_ascii=False), labels
    return choices[picked[0]], labels


def _without_recommended(choice: str) -> str:
    stripped = choice.strip()
    if stripped.casefold().endswith(RECOMMENDED_LABEL.casefold()):
        return stripped[: -len(RECOMMENDED_LABEL)].strip()
    return stripped


def _words(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", text.lower())


def _picked_choices(text: str, choices: list[str], multi_select: bool) -> list[int]:
    said = _words(text)
    if not said:
        return []
    labels = [_words(_without_recommended(choice)) for choice in choices]
    # The whole answer is one choice's label.
    if labels.count(said) == 1:
        return [labels.index(said)]
    # Never past a negation: "not the recommended one".
    if any(word in _NEGATIONS for word in said):
        return []
    if any(word.startswith("recommend") or word == "suggested" for word in said):
        marked = [index for index, choice in enumerate(choices) if choice.strip().lower().endswith(RECOMMENDED_LABEL.lower())]
        if len(marked) == 1:
            return marked
    # Numbers, when the words are only numbers and the words around them;
    # "one" after another word ("the second one") stands for a choice.
    counted = [index for index, word in enumerate(said)
               if word != "one" or index == 0 or said[index - 1] in _ONE_LEADS]
    numbers = [_NUMBER_WORDS.get(said[index]) or (int(said[index]) if said[index].isdecimal() else None)
               for index in counted]
    numbers = [number for number in numbers if number is not None]
    filler = all(word in _NUMBER_WORDS or word.isdecimal() or word in _NUMBER_FILLER for word in said)
    if filler and numbers:
        seen = list(dict.fromkeys(numbers))
        if not all(1 <= number <= len(choices) for number in seen):
            return []
        if not multi_select and len(seen) != 1:
            return []
        return [number - 1 for number in seen]
    # A label said within a few more words ("staging, please").
    contained = [index for index, label in enumerate(labels)
                 if label and len(" ".join(label)) >= 3 and len(said) <= len(label) + 4 and _contains(said, label)]
    if len(contained) == 1 or (multi_select and contained):
        return contained
    return []


def _contains(words: list[str], part: list[str]) -> bool:
    return any(words[start:start + len(part)] == part for start in range(len(words) - len(part) + 1))

def _session_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("session_ids must be a list")
    ids: list[str] = []
    for item in value:
        if not isinstance(item, str) or not _session_key(item.strip()):
            raise ValueError("session_ids must be session ids")
        if item.strip() not in ids:
            ids.append(item.strip())
    if not ids:
        raise ValueError("session_ids must name the job's session")
    return ids[:MAX_WATCH_SESSIONS]


def _session_key(value: Any) -> str:
    """``value`` unchanged when it is a session key, else "" (events.session_key)."""
    if not isinstance(value, str) or not value or _SESSION_KEY_CONTROL.search(value):
        return ""
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:  # a lone surrogate
        return ""
    return value if size <= SESSION_KEY_MAX_BYTES else ""


def _seconds(value: Any, name: str, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= maximum:
        raise ValueError(f"{name} must be a whole number of seconds from 0 to {maximum}")
    return value


def _title(value: Any) -> str:
    text = " ".join(value.split()) if isinstance(value, str) else ""
    return text[:MAX_TITLE_CHARS]


def _reason(value: Any) -> str:
    text = " ".join(value.split()) if isinstance(value, str) else ""
    return text[:MAX_REASON_CHARS]


def _rules(value: Any) -> str | None:
    """The user's call rules as kept: lines trimmed, blank lines and control
    characters dropped. None when ``value`` isn't text or is too long."""
    if not isinstance(value, str):
        return None
    lines = ("".join(char for char in line if char.isprintable()).strip() for line in value.splitlines())
    text = "\n".join(line for line in lines if line)
    return text if len(text) <= MAX_RULES_CHARS else None


def _place_ids(value: Any) -> list[str] | None:
    """Saved place ids, deduplicated in order. None when malformed."""
    if not isinstance(value, list) or len(value) > MAX_AVOID_PLACES:
        return None
    if any(not isinstance(item, str) or not _PLACE_ID.match(item) for item in value):
        return None
    return list(dict.fromkeys(value))


@contextmanager
def _exclusive(handle: Any) -> Iterator[None]:
    # The same locking as client.state_file_lock: flock where there is one,
    # a first-byte lock on Windows.
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
