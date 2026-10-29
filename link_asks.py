"""Asks between linked agents, for Linked Rooms (designs/cross-owner-agents, N9).

Outside any room, your agent can ask a linked person's Hermes something (``conduit_ask_linked``, link_ask_tool.py):
whether they're free on a date, or a question. The ask goes over the Link (ask.ask) and the answer comes back
(ask.answer). On the asked side no agent ever reads it: a standing rule the owner set for that Link answers it,
computed here, or the owner does, from their phone (a confirm prompt in the present store with the Link,
``present_store.LINK_ASK_FEATURE``) or the CLI. So nothing the asker writes steers anyone's agent there.

The rules, per Link: whether they're free on a date ``yes_no`` (free or busy, without asking), ``times`` (also the
busy times then), ``ask`` (the owner sees the answer the rule would give and shares it, says something else, or
doesn't) or ``off``; a question ``ask`` or ``off``; and the profile whose calendar and phone answer. A Link without
rules has the defaults (``DEFAULT_RULES``): nothing goes without the owner.

Both sides keep what they need in ``conduit-rooms/asks.json`` in the root Hermes home, owner only, under a file
lock: the rules, the asks received and how each went, the asks sent and their answers, and the envelopes still to
go. The asks loop (``start``), where the Link loop runs, sends them and the owner's answers once given.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, time as day_time, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import room_identity, room_links

logger = logging.getLogger(__name__)

VERSION = 1
ASK, ANSWER = "ask.ask", "ask.answer"
KINDS = (ASK, ANSWER)
DIRECTORY = room_identity.DIRECTORY
FILE_NAME = "asks.json"

FREE_ON_RULES = ("yes_no", "times", "ask", "off")
QUESTION_RULES = ("ask", "off")
DEFAULT_RULES = {"free_on": "ask", "questions": "ask", "profile": "default"}
MAX_QUESTION_CHARS = 300
MAX_TEXT_CHARS = 2000
MAX_AGENT_CHARS = 64
# Busy times an answer carries, on both sides, so its text and its spans agree.
MAX_BUSY_SPANS = 48
# On the asked side: asks a Link may make an hour, and how many may wait for the owner, a Link and in all.
PER_HOUR = 10
WAITING_PER_LINK = 3
WAITING_ALL = 20
HOUR_S = 3600
# The furthest a date asked about may be, as conduit_calendar reads.
MAX_DAYS_AHEAD = 92
# How long an ask waits for its owner.
ASK_TTL_MINUTES = 24 * 60
# On the asking side, an ask with no answer this long after it went wasn't answered: the owner's day, and the
# mailbox's slack.
UNANSWERED_S = ASK_TTL_MINUTES * 60 + 2 * 3600
# Finished asks, kept for the tool's answers and the CLI.
KEPT_S = 7 * 86400
MAX_SENT = 200
MAX_RECEIVED = 200
# An envelope still to go is dropped once the mailbox would have dropped it.
LASTS_S = room_links.HOLD_S
IDLE_S = 2
MAX_BACKOFF_S = 300

# How an ask went, as ask.answer says it.
ANSWERED, DECLINED, NOT_ALLOWED, TOO_MANY, UNAVAILABLE, UNANSWERED, BAD_ASK = (
    "answered", "declined", "not_allowed", "too_many", "unavailable", "unanswered", "bad_ask")
OUTCOMES = frozenset({ANSWERED, DECLINED, NOT_ALLOWED, TOO_MANY, UNAVAILABLE, UNANSWERED, BAD_ASK})
# On the asking side, an ask also waits: sending (not yet taken by the mailbox) and sent.
SENDING, SENT = "sending", "sent"

_ID = re.compile(r"[A-Za-z0-9_-]{8,48}")
_HHMM = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]")
_PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_thread_lock = threading.Lock()


class AsksError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


# --- What's asked ------------------------------------------------------------------------------


def free_on(value: Any) -> Optional[dict[str, str]]:
    """A free-on-a-date ask, cleaned: ``date`` (YYYY-MM-DD) and, together, ``from`` and ``to`` (HH:MM, from before
    to), or None when it's malformed."""
    if not isinstance(value, dict) or not isinstance(value.get("date"), str):
        return None
    try:
        day = date.fromisoformat(value["date"].strip())
    except ValueError:
        return None
    asked = {"date": day.isoformat()}
    start, end = value.get("from"), value.get("to")
    if start in (None, "") and end in (None, ""):
        return asked
    if not (isinstance(start, str) and _HHMM.fullmatch(start.strip()) and isinstance(end, str)
            and _HHMM.fullmatch(end.strip()) and start.strip() < end.strip()):
        return None
    return {**asked, "from": start.strip(), "to": end.strip()}


def question(value: Any) -> str:
    """A question, on one line, or "" when there's none or it's longer than MAX_QUESTION_CHARS."""
    text = _line(value, MAX_QUESTION_CHARS + 1)
    return text if len(text) <= MAX_QUESTION_CHARS else ""


def described(asked: dict[str, Any]) -> str:
    """What's asked, on one line: "free on 2026-10-22, 10:00-12:00?" or the question."""
    if not isinstance(asked, dict):
        return "an ask that couldn't be read"
    if "free_on" in asked:
        when = asked["free_on"]
        window = f", {when['from']}-{when['to']}" if "from" in when else ""
        return f"free on {when['date']}{window}?"
    return asked["question"]


def rules_of(value: Any) -> dict[str, str]:
    """A Link's rules, cleaned: what's missing or unknown is the default."""
    value = value if isinstance(value, dict) else {}
    profile = value.get("profile")
    return {"free_on": value.get("free_on") if value.get("free_on") in FREE_ON_RULES else DEFAULT_RULES["free_on"],
            "questions": value.get("questions") if value.get("questions") in QUESTION_RULES
            else DEFAULT_RULES["questions"],
            "profile": profile if isinstance(profile, str) and _PROFILE.fullmatch(profile)
            else DEFAULT_RULES["profile"]}


# --- The asks of one installation ---------------------------------------------------------------


class Asks:
    """Asks over the Links of the installation at ``root``: ``send`` is Links.send, ``link(link_id)`` the Link as
    Links.get shows it, ``links()`` every Link as Links.view lists them, ``present(profile)`` a profile's
    PresentStore and ``phone(profile)`` its PhoneStore."""

    def __init__(self, root: Any, *, send: Optional[Callable[..., Any]] = None,
                 link: Optional[Callable[[str], Optional[dict]]] = None,
                 links: Optional[Callable[[], list[dict]]] = None, clock: Callable[[], float] = time.time,
                 present: Optional[Callable[[str], Any]] = None, phone: Optional[Callable[[str], Any]] = None) -> None:
        self.root = Path(root)
        self.store = Store(self.root)
        own = None if send is not None and link is not None and links is not None else room_links.Links(self.root)
        self.send = send if send is not None else own.send
        self.link = link if link is not None else own.get
        self.links = links if links is not None else (lambda: own.view()["links"])
        self.clock = clock
        self.present = present if present is not None else self._present
        self.phone = phone if phone is not None else self._phone

    # --- The owner's rules ------------------------------------------------------------------------

    def rules(self) -> dict[str, Any]:
        """Each Link's rules, the defaults where none were set, and the defaults."""
        saved = self.store.read()["rules"]
        return {"links": [{"id": link["id"], "name": link["name"], "status": link["status"],
                           "rules": rules_of(saved.get(link["id"])), "saved": link["id"] in saved}
                          for link in self.links()],
                "defaults": dict(DEFAULT_RULES)}

    def set_rules(self, link_id: Any, changes: Any) -> dict[str, Any]:
        """Changes a Link's rules: only what ``changes`` names, each checked."""
        if not isinstance(link_id, str) or self.link(link_id) is None:
            raise AsksError("not_found", "There's no such Link", 404)
        if not isinstance(changes, dict) or not changes or set(changes) - set(DEFAULT_RULES):
            raise AsksError("invalid", "Change free_on, questions or profile")
        if "free_on" in changes and changes["free_on"] not in FREE_ON_RULES:
            raise AsksError("invalid", f"free_on is one of {', '.join(FREE_ON_RULES)}")
        if "questions" in changes and changes["questions"] not in QUESTION_RULES:
            raise AsksError("invalid", f"questions is one of {', '.join(QUESTION_RULES)}")
        if "profile" in changes and not (isinstance(changes["profile"], str)
                                         and _PROFILE.fullmatch(changes["profile"])
                                         and self._profile_exists(changes["profile"])):
            raise AsksError("invalid", "profile is one of this Hermes' profiles")
        with self.store.update() as state:
            rules = {**rules_of(state["rules"].get(link_id)), **changes}
            state["rules"][link_id] = rules
        return {"id": link_id, "rules": rules}

    # --- Asking ---------------------------------------------------------------------------------

    def ask(self, link_id: Any, asked: dict[str, Any], *, profile: str, session_id: str, agent: str
            ) -> dict[str, Any]:
        """Keeps an ask to go over a Link (the asks loop sends it): ``asked`` is ``{"free_on": ...}`` or
        ``{"question": ...}``, already cleaned. The ask as it stands."""
        link = self.link(link_id) if isinstance(link_id, str) else None
        if link is None or link.get("status") != "linked":
            raise AsksError("not_linked", "This Hermes isn't linked with that one", 404)
        now = int(self.clock())
        ask_id = secrets.token_urlsafe(12)
        body = {"v": VERSION, "ask_id": ask_id, **asked, "agent": _line(agent, MAX_AGENT_CHARS) or "Hermes"}
        with self.store.update() as state:
            recent = [ask for ask in state["sent"].values() if ask["link"] == link_id and now - ask["at"] < HOUR_S]
            if len(recent) >= PER_HOUR:
                raise AsksError("too_many", f"{PER_HOUR} asks went to {link['name']}'s Hermes this hour.", 429)
            state["sent"][ask_id] = {"link": link_id, "name": link["name"], "asked": asked, "profile": profile,
                                     "session_id": session_id, "at": now, "status": SENDING, "heard": False}
            _queue(state, link_id, ASK, ask_id, body, now)
        return self.sent(ask_id)

    def sent(self, ask_id: Any) -> Optional[dict[str, Any]]:
        """An ask this side sent, as it stands."""
        ask = self.store.read()["sent"].get(ask_id) if isinstance(ask_id, str) else None
        return _shown_sent(ask_id, ask) if ask is not None else None

    def answers_for(self, profile: str, session_id: Any) -> list[dict[str, Any]]:
        """The finished asks a chat sent that it hasn't heard, oldest first. ``session_id``: its session, or the
        sessions it is (the one it continues through compactions too)."""
        sessions = _sessions(session_id)
        found = [(ask_id, ask) for ask_id, ask in self.store.read()["sent"].items()
                 if ask["profile"] == profile and ask["session_id"] in sessions and not ask["heard"]
                 and ask["status"] in OUTCOMES]
        return [_shown_sent(ask_id, ask) for ask_id, ask in sorted(found, key=lambda item: item[1]["at"])]

    def recent(self, profile: str, session_id: Any = None) -> list[dict[str, Any]]:
        """A profile's asks, a chat's when ``session_id`` is given (as answers_for), newest first."""
        sessions = None if session_id is None else _sessions(session_id)
        found = [(ask_id, ask) for ask_id, ask in self.store.read()["sent"].items() if ask["profile"] == profile
                 and (sessions is None or ask["session_id"] in sessions)]
        return [_shown_sent(ask_id, ask) for ask_id, ask in sorted(found, key=lambda item: -item[1]["at"])]

    def mark_heard(self, ask_ids: Any) -> None:
        ids = {ask_id for ask_id in ask_ids if isinstance(ask_id, str)} if isinstance(ask_ids, (list, set, tuple)) \
            else set()
        if not ids:
            return
        with self.store.update() as state:
            for ask_id in ids & set(state["sent"]):
                if state["sent"][ask_id]["status"] in OUTCOMES:
                    state["sent"][ask_id]["heard"] = True

    # --- Envelopes ------------------------------------------------------------------------------

    def handle(self, received: room_links.Received) -> None:
        if received.kind == ASK:
            self.on_ask(received)
        elif received.kind == ANSWER:
            self.on_answer(received)

    def on_ask(self, received: room_links.Received) -> None:
        """An ask from a linked Hermes: answered by its Link's rules, held for the owner, or refused. Again, the
        answer it got."""
        body = received.body if isinstance(received.body, dict) else {}
        ask_id = body.get("ask_id")
        if not isinstance(ask_id, str) or not _ID.fullmatch(ask_id):
            logger.info("Linked Rooms dropped an ask from Link %s without an ask id", received.link_id)
            return
        key = _received_key(received.link_id, ask_id)
        state = self.store.read()
        if key in state["received"]:
            return
        link = self.link(received.link_id) or {}
        name = _line(link.get("name"), room_links.NAME_MAX) or "Someone"
        rules = rules_of(state["rules"].get(received.link_id))
        asked = _asked_from(body)
        now = int(self.clock())
        entry = {"link": received.link_id, "ask_id": ask_id, "name": name, "agent": _line(body.get("agent"),
                 MAX_AGENT_CHARS) or "Hermes", "asked": asked, "profile": rules["profile"], "at": now}
        if asked is None:
            self._settle(entry, {"status": BAD_ASK, "text": "That ask can't be read."})
            return
        recent = [other for other in state["received"].values()
                  if other["link"] == received.link_id and now - other["at"] < HOUR_S]
        if len(recent) >= PER_HOUR:
            self._settle(entry, {"status": TOO_MANY, "text": f"At most {PER_HOUR} asks an hour."})
            return
        rule = rules["free_on"] if "free_on" in asked else rules["questions"]
        if rule == "off":
            self._settle(entry, {"status": NOT_ALLOWED, "text": "They don't take asks like this."})
            return
        if not self._profile_exists(rules["profile"]):
            # Its profile was deleted since: nothing is written there, so it isn't made again.
            self._settle(entry, {"status": UNAVAILABLE, "text": "They can't be asked right now."})
            return
        computed = self._free(rules["profile"], asked["free_on"], times=rule == "times") if "free_on" in asked \
            else None
        if computed is not None and computed["status"] != ANSWERED:
            self._settle(entry, computed)
            return
        if rule in ("yes_no", "times"):
            self._settle(entry, {**computed, "by": "rule"})
            return
        waiting = [other for other in state["received"].values() if other.get("status") == "held"]
        if len([other for other in waiting if other["link"] == received.link_id]) >= WAITING_PER_LINK \
                or len(waiting) >= WAITING_ALL:
            self._settle(entry, {"status": TOO_MANY, "text": "Too many asks are waiting for an answer."})
            return
        self._hold(entry, computed)

    def on_answer(self, received: room_links.Received) -> None:
        """The answer to an ask this side sent, over the Link it went on; again, or another's, is dropped."""
        body = received.body if isinstance(received.body, dict) else {}
        ask_id, status = body.get("ask_id"), body.get("status")
        if not isinstance(ask_id, str) or status not in OUTCOMES:
            return
        answer = {"status": status, "text": _text(body.get("text"), MAX_TEXT_CHARS),
                  "by": body.get("by") if body.get("by") in ("rule", "owner") else None}
        if isinstance(body.get("free"), bool):
            answer["free"] = body["free"]
        busy = _busy(body.get("busy"))
        if busy is not None:
            answer["busy"] = busy
        if _is_zone(body.get("time_zone")):
            answer["time_zone"] = body["time_zone"]
        with self.store.update() as state:
            ask = state["sent"].get(ask_id)
            if ask is None or ask["link"] != received.link_id or ask["status"] in OUTCOMES:
                return
            ask.update(answer, answered_at=int(self.clock()))
            ask["answer"] = ask.pop("text")

    # --- The asked side's owner --------------------------------------------------------------------

    def held(self) -> list[dict[str, Any]]:
        """The asks waiting for this side's owner, oldest first."""
        found = [entry for entry in self.store.read()["received"].values() if entry.get("status") == "held"]
        return [_shown_received(entry) for entry in sorted(found, key=lambda entry: entry["at"])]

    def received(self) -> list[dict[str, Any]]:
        """Every ask this side got that it still keeps, newest first."""
        found = self.store.read()["received"].values()
        return [_shown_received(entry) for entry in sorted(found, key=lambda entry: -entry["at"])]

    def answer_held(self, ask_id: Any, *, text: Any = None, share: bool = False, decline: bool = False
                    ) -> dict[str, Any]:
        """The owner answers a held ask from the CLI: with ``text``, with what the rule would say (``share``), or
        not (``decline``). Its prompt on the phone is settled the same way."""
        if [bool(text), share, decline].count(True) != 1:
            raise AsksError("invalid", "Answer with text, share or decline")
        entry = next((entry for entry in self.store.read()["received"].values()
                      if entry["ask_id"] == ask_id and entry.get("status") == "held"), None)
        if entry is None:
            raise AsksError("not_found", "No ask with that id waits for you", 404)
        if share and entry.get("computed") is None:
            raise AsksError("invalid", "A question has nothing to share: answer with text or decline")
        written = _text(text, MAX_TEXT_CHARS) if text else ""
        if text and not written:
            raise AsksError("invalid", "Write the answer")
        outcome = "edit" if written else "confirmed" if share else "declined"
        try:
            self.present(entry["profile"]).settle_link(entry["prompt_id"], outcome, written)
        except Exception:  # noqa: BLE001 — the answer goes all the same; the phone's prompt expires
            logger.debug("Linked Rooms couldn't settle an ask's prompt", exc_info=True)
        if not self._settle(entry, _owners(entry, outcome, written)):
            raise AsksError("answered", "That ask was already answered", 409)
        return _shown_received({**entry, "status": "answered"})

    # --- The loop ----------------------------------------------------------------------------------

    def pass_once(self) -> None:
        """The owner's answers given since, the asks sent that were never answered, then what's still to go."""
        if not self.store.path.exists():
            return
        self._collect()
        self._expire()
        self.flush()

    def flush(self) -> None:
        """Sends the envelopes still to go; one the mailbox didn't take stays for a later pass, until it would have
        run out there, and one for a Link that ended goes."""
        now = int(self.clock())
        for item in self.store.read()["outbox"]:
            gone, went = item["until"] <= now, False
            if not gone:
                try:
                    self.send(item["link"], item["kind"], item["body"], message_id=item["id"])
                    went = True
                except room_links.LinkError as exc:
                    if exc.code != "not_linked":
                        logger.info("Linked Rooms will send an %s over Link %s later (%s)", item["kind"],
                                    item["link"], exc.code)
                        continue
                    gone = True
            with self.store.update() as state:
                state["outbox"] = [other for other in state["outbox"]
                                   if (other["id"], other["link"]) != (item["id"], item["link"])]
                ask = state["sent"].get(item["body"].get("ask_id")) if item["kind"] == ASK else None
                if ask is not None and ask["status"] == SENDING:
                    if went:
                        ask["status"] = SENT
                    else:
                        ask.update(status=UNAVAILABLE, answer="It couldn't be sent.", by="local", answered_at=now)

    def _collect(self) -> None:
        """Sends the answers the owner gave on their phone to the asks held for them."""
        for key in [key for key, entry in self.store.read()["received"].items() if entry.get("status") == "held"]:
            raw = self.store.read()["received"].get(key)
            if raw is None or raw.get("status") != "held":
                continue
            if not self._profile_exists(raw["profile"]):
                # Its profile was deleted while the ask waited: nothing is written there, so it isn't made again.
                self._settle(raw, {"status": UNAVAILABLE, "text": "They can't be asked right now."})
                continue
            try:
                present = self.present(raw["profile"])
                prompt = present.link_ask(raw["prompt_id"])
            except Exception:  # noqa: BLE001 — looked at again on the next pass
                logger.debug("Linked Rooms couldn't read an ask's prompt", exc_info=True)
                continue
            if prompt is None:
                self._settle(raw, {"status": UNANSWERED, "text": "Not answered."})
                continue
            if prompt["status"] == "waiting":
                continue
            self._settle(raw, _owners(raw, prompt["status"], prompt.get("edit_text") or ""))
            try:
                present.mark_heard([raw["prompt_id"]])
            except Exception:  # noqa: BLE001
                logger.debug("Linked Rooms couldn't mark an ask's prompt answered", exc_info=True)

    def _expire(self) -> None:
        now = int(self.clock())
        with self.store.update() as state:
            for ask in state["sent"].values():
                if ask["status"] == SENT and now - ask["at"] > UNANSWERED_S:
                    ask.update(status=UNANSWERED, answer="No answer came.", by="local", answered_at=now)
            _forget_old(state, now)

    # --- Answering ---------------------------------------------------------------------------------

    def _free(self, profile: str, asked: dict[str, str], *, times: bool) -> dict[str, Any]:
        """Whether the profile's owner is free then, from the calendar their phone shares with it: ``free``, the
        busy times then when ``times``, and the text that says it; or why it can't tell."""
        from .phone_store import PhoneError
        from . import phone_tool

        cant = {"status": UNAVAILABLE, "text": "Their calendar can't be checked from here."}
        try:
            store = self.phone(profile)
            with store.reader("events") as opened:
                if opened is None:
                    return cant
                db, device = opened
                tz = device["time_zone"]
                zone = phone_tool._zone(tz)
                day = date.fromisoformat(asked["date"])
                today = datetime.fromtimestamp(store.clock(), zone).date()
                if not timedelta(days=-1) <= day - today <= timedelta(days=MAX_DAYS_AHEAD):
                    return {"status": UNAVAILABLE, "text": f"Only dates up to {MAX_DAYS_AHEAD} days ahead."}
                start = datetime.combine(day, day_time.fromisoformat(asked.get("from", "00:00")), zone)
                end = datetime.combine(day, day_time.fromisoformat(asked["to"]), zone) if "to" in asked \
                    else datetime.combine(day + timedelta(days=1), day_time(0, 0), zone)
                start_s, end_s = int(start.timestamp()), int(end.timestamp())
                window = (_epoch(device["window_from"]), _epoch(device["window_to"]))
                if device["window_from"] and (start_s < window[0] or end_s > window[1]):
                    return {"status": UNAVAILABLE, "text": "That date is past what their phone shares."}
                blocks = store.busy_blocks(db, device["id"], start_s, end_s)
                synced = device.get("last_seen")
                now = store.clock()
        except PhoneError:
            return cant
        except Exception as exc:  # noqa: BLE001 — answered all the same, so the ask isn't dropped
            logger.warning("Linked Rooms couldn't read a calendar for an ask (%s)", type(exc).__name__)
            return cant
        when = described({"free_on": asked}).removeprefix("free on ").removesuffix("?")
        answer: dict[str, Any] = {"status": ANSWERED, "free": not blocks, "time_zone": tz}
        if blocks and times:
            answer["busy"] = [{"start": _clock(a, zone), "end": _clock(b, zone)} for a, b in blocks[:MAX_BUSY_SPANS]]
            spans = ", ".join(f"{span['start']}-{span['end']}" for span in answer["busy"])
            more = len(blocks) - len(answer["busy"])
            answer["text"] = (f"Busy {spans} on {asked['date']} ({tz}), "
                              + (f"and {more} more busy times; " if more else "") + "free otherwise.")
        else:
            answer["text"] = f"{'Free' if not blocks else 'Busy'} on {when}."
        stale = _stale(synced, now)
        if stale:
            answer["text"] += f" Their phone last shared its calendar {stale}."
        return answer

    def _hold(self, entry: dict[str, Any], computed: Optional[dict[str, Any]]) -> None:
        """Puts the ask on its owner's phone, with what the rule would say to share; none that shows it: answered
        unavailable."""
        share = computed is not None
        title = _title(entry["asked"])
        # The question is the other side's words: shown as plain text, never as Markdown.
        preview = computed["text"] if share else _plain(entry["asked"]["question"])
        try:
            prompt = self.present(entry["profile"]).ask_link(
                title, preview, link={"id": entry["link"], "name": entry["name"], "share": share},
                ttl_minutes=ASK_TTL_MINUTES)
        except Exception as exc:  # noqa: BLE001 — answered at once, so the asker isn't left waiting
            logger.info("Linked Rooms couldn't put an ask on its owner's phone (%s)", getattr(exc, "code", exc))
            self._settle(entry, {"status": UNAVAILABLE, "text": "They can't be asked right now."})
            return
        with self.store.update() as state:
            key = _received_key(entry["link"], entry["ask_id"])
            again = key in state["received"]
            if not again:
                state["received"][key] = {**entry, "status": "held", "prompt_id": prompt["id"],
                                          **({"computed": computed} if share else {})}
        if again:
            # The same ask, handled twice at once: the other one's prompt stands, so this one leaves the phone.
            try:
                self.present(entry["profile"]).settle_link(prompt["id"], "declined")
            except Exception:  # noqa: BLE001
                logger.debug("Linked Rooms couldn't take back a second prompt for an ask", exc_info=True)
            return
        self._notify(entry, prompt["id"])

    def _notify(self, entry: dict[str, Any], prompt_id: str) -> None:
        """Wakes the owner's phone for the ask, as any prompt's wake does (room_asks._nudge). Never fails the ask."""
        try:
            from . import client, session_titles
            from .events import push_event

            client.enqueue(push_event("phone.changes", identifier=f"present:{prompt_id}", profile=entry["profile"],
                                      profile_name=session_titles.bot_name(), title=f"{entry['name']}'s Hermes asks",
                                      body=_title(entry["asked"]), mode="alert"))
        except Exception:  # noqa: BLE001
            logger.warning("Linked Rooms couldn't wake the phone for an ask", exc_info=True)

    def _settle(self, entry: dict[str, Any], answer: dict[str, Any]) -> bool:
        """Records how an ask went and queues its answer; False when it was already answered."""
        now = int(self.clock())
        body = {"v": VERSION, "ask_id": entry["ask_id"], "status": answer["status"],
                "text": _text(answer.get("text"), MAX_TEXT_CHARS)}
        for field in ("by", "free", "busy", "time_zone"):
            if answer.get(field) is not None:
                body[field] = answer[field]
        with self.store.update() as state:
            key = _received_key(entry["link"], entry["ask_id"])
            kept = state["received"].get(key)
            if kept is not None and kept.get("status") != "held":
                return False
            state["received"][key] = {**{k: v for k, v in entry.items() if k not in ("computed",)},
                                      "status": answer["status"], "answer": body["text"], "answered_at": now}
            _queue(state, entry["link"], ANSWER, entry["ask_id"], body, now)
        return True

    # --- Hermes ------------------------------------------------------------------------------------

    def _present(self, profile: str) -> Any:
        from .present_store import PresentStore

        return PresentStore(self._profile_home(profile))

    def _phone(self, profile: str) -> Any:
        from .phone_store import PhoneStore

        return PhoneStore(self._profile_home(profile))

    def _profile_home(self, profile: str) -> Path:
        return self.root if profile == "default" else self.root / "profiles" / profile

    def _profile_exists(self, profile: str) -> bool:
        """The default, or a named profile Hermes keeps: a folder under profiles/ with something that marks it one
        (as room_invites reads them)."""
        from . import room_invites

        home = self.root / "profiles" / profile
        return profile == "default" or (home.is_dir() and any((home / marker).exists()
                                                              for marker in room_invites._PROFILE_MARKERS))


# --- Where it runs -------------------------------------------------------------------------------


_running: dict[str, Asks] = {}
_running_lock = threading.Lock()


def start(root: Any) -> Asks:
    """The asks of the installation at ``root``, started in this process: the handlers for their envelopes on this
    copy of room_links, which run where its loop reads the inbox, and the asks loop. Again, the asks already
    started."""
    key = str(Path(root))
    with _running_lock:
        asks = _running.get(key)
        if asks is not None:
            return asks
        asks = _running[key] = Asks(root)
        for kind in KINDS:
            room_links.handles(kind)(_on_envelope)
    threading.Thread(target=_loop, args=(asks,), name="conduit-link-asks", daemon=True).start()
    return asks


def _on_envelope(received: room_links.Received) -> None:
    asks = _running.get(str(received.root)) if received.root is not None else None
    if asks is None:
        logger.info("Linked Rooms dropped an %s for an installation whose asks aren't running here", received.kind)
        return
    asks.handle(received)


def _loop(asks: Asks, sleep: Callable[[float], None] = time.sleep) -> None:
    backoff = 0
    while True:
        try:
            asks.pass_once()
            backoff = 0
            sleep(IDLE_S)
        except Exception as exc:  # noqa: BLE001 - the loop outlives any one pass
            backoff = min(MAX_BACKOFF_S, max(5, backoff * 2))
            logger.warning("Linked Rooms couldn't look at its asks (%s); trying again in %d s",
                           getattr(exc, "code", type(exc).__name__), backoff)
            sleep(backoff)


# --- The store -----------------------------------------------------------------------------------


class Store:
    """``<root>/conduit-rooms/asks.json``."""

    def __init__(self, root: Any) -> None:
        self.directory = Path(root) / DIRECTORY
        self.path = self.directory / FILE_NAME

    def read(self) -> dict[str, Any]:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return _empty()
        except OSError as exc:
            raise AsksError("unreadable", f"Couldn't read {self.path} ({exc.strerror or exc})", 500) from None
        try:
            state = json.loads(raw)
        except (ValueError, RecursionError):
            state = None
        if not _well_formed(state):
            raise AsksError("unreadable", f"{self.path} isn't an asks file this plugin wrote", 500)
        return state

    @contextmanager
    def update(self) -> Iterator[dict[str, Any]]:
        """The state to change, saved when the block ends without raising."""
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        lock_path = self.directory / f".{FILE_NAME}.lock"
        with _thread_lock, open(lock_path, "a+b") as handle, room_identity._exclusive(handle):
            state = self.read()
            before = json.dumps(state, sort_keys=True)
            yield state
            if json.dumps(state, sort_keys=True) != before:
                self._save(state)

    def _save(self, state: dict[str, Any]) -> None:
        encoded = json.dumps(state, separators=(",", ":"), sort_keys=True, ensure_ascii=False) + "\n"
        descriptor, name = tempfile.mkstemp(prefix=f".{FILE_NAME}.", suffix=".tmp", dir=self.directory)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        self.path.chmod(0o600)
        room_identity._sync_directory(self.directory)


def _empty() -> dict[str, Any]:
    return {"version": VERSION, "rules": {}, "received": {}, "sent": {}, "outbox": []}


def _well_formed(state: Any) -> bool:
    return (isinstance(state, dict) and state.get("version") == VERSION
            and all(isinstance(state.get(key), dict) for key in ("rules", "received", "sent"))
            and isinstance(state.get("outbox"), list))


def _queue(state: dict[str, Any], link_id: str, kind: str, ask_id: str, body: dict, now: int) -> None:
    message_id = f"{kind}:{ask_id}"
    if all((item["id"], item["link"]) != (message_id, link_id) for item in state["outbox"]):
        state["outbox"].append({"id": message_id, "link": link_id, "kind": kind, "body": body,
                                "until": now + LASTS_S})


def _forget_old(state: dict[str, Any], now: int) -> None:
    """Finished asks past KEPT_S go, and the oldest past the caps; a held one stays while it waits."""
    for section, cap in (("sent", MAX_SENT), ("received", MAX_RECEIVED)):
        entries = state[section]
        done = sorted((key for key, entry in entries.items() if entry.get("status") not in (SENDING, SENT, "held")),
                      key=lambda key: entries[key]["at"])
        for key in done:
            if now - entries[key].get("answered_at", entries[key]["at"]) > KEPT_S:
                del entries[key]
        extra = len(entries) - cap
        for key in [key for key in done if key in entries][:max(0, extra)]:
            del entries[key]


def _received_key(link_id: str, ask_id: str) -> str:
    return f"{link_id}:{ask_id}"


def _asked_from(body: dict[str, Any]) -> Optional[dict[str, Any]]:
    """What an ask asks: exactly one of free_on and question, cleaned, or None."""
    if ("free_on" in body) == ("question" in body):
        return None
    if "free_on" in body:
        when = free_on(body["free_on"])
        return {"free_on": when} if when is not None else None
    text = question(body["question"])
    return {"question": text} if text else None


def _owners(entry: dict[str, Any], outcome: str, written: str) -> dict[str, Any]:
    """The answer from the owner's outcome on a held ask."""
    if outcome == "edit" and written:
        return {"status": ANSWERED, "by": "owner", "text": written}
    if outcome == "confirmed" and entry.get("computed"):
        return {**entry["computed"], "by": "owner"}
    if outcome == "expired":
        return {"status": UNANSWERED, "text": "Not answered in time."}
    return {"status": DECLINED, "text": "They didn't share that."}


def _title(asked: dict[str, Any]) -> str:
    """The prompt's title: what's asked, within the present store's 80 characters."""
    text = ("Free on " + described(asked).removeprefix("free on ")) if "free_on" in asked else asked["question"]
    return text if len(text) <= 80 else text[:79].rstrip() + "…"


def _shown_sent(ask_id: str, ask: dict[str, Any]) -> dict[str, Any]:
    shown = {"id": ask_id, "to": ask["name"], "link": ask["link"], "asked": ask["asked"], "status": ask["status"],
             "at": ask["at"]}
    for field in ("answer", "by", "free", "busy", "time_zone", "answered_at"):
        if ask.get(field) not in (None, ""):
            shown[field] = ask[field]
    return shown


def _shown_received(entry: dict[str, Any]) -> dict[str, Any]:
    shown = {"id": entry["ask_id"], "link": entry["link"], "from": entry["name"], "agent": entry["agent"],
             "asked": entry["asked"], "status": entry["status"], "profile": entry["profile"], "at": entry["at"]}
    if entry.get("computed"):
        shown["share"] = entry["computed"]["text"]
    for field in ("answer", "answered_at"):
        if entry.get(field) not in (None, ""):
            shown[field] = entry[field]
    return shown


_MARKDOWN = re.compile(r"([\\`*_{}\[\]()#+!<>|~])")
STALE_S = 6 * 3600


def _sessions(value: Any) -> set[str]:
    return {value} if isinstance(value, str) else {item for item in value if isinstance(item, str)}


def _is_zone(value: Any) -> bool:
    """A time zone's IANA name, which is all the other side's answer may say its zone is."""
    if not isinstance(value, str) or not 0 < len(value) <= 64:
        return False
    try:
        ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError):
        return False
    return True


def _plain(text: str) -> str:
    """Text shown as itself where Markdown is read: each Markdown character escaped."""
    return _MARKDOWN.sub(r"\\\1", text)


def _stale(synced: Any, now: float) -> str:
    """How long ago the phone last shared its calendar, when it's over STALE_S; else ""."""
    try:
        seconds = int(now) - int(synced)
    except (TypeError, ValueError):
        return ""
    if seconds <= STALE_S:
        return ""
    return f"{seconds // 3600} hours ago" if seconds < 2 * 86400 else f"{seconds // 86400} days ago"


def _busy(value: Any) -> Optional[list[dict[str, str]]]:
    if not isinstance(value, list):
        return None
    spans = [{"start": span["start"], "end": span["end"]} for span in value[:MAX_BUSY_SPANS]
             if isinstance(span, dict) and all(isinstance(span.get(key), str) and _HHMM.fullmatch(span[key])
                                               for key in ("start", "end"))]
    return spans


def _clock(epoch: int, zone: Any) -> str:
    return datetime.fromtimestamp(epoch, zone).strftime("%H:%M")


def _epoch(value: Any) -> int:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    try:
        return int(datetime.fromisoformat(str(value)).timestamp())
    except ValueError:
        return 0


def _line(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join("".join(char if char.isprintable() else " " for char in value).split())[:limit]


def _text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    lines = value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    kept = ["".join(char if char.isprintable() else " " for char in line).rstrip() for line in lines]
    return "\n".join(kept).strip()[:limit]
