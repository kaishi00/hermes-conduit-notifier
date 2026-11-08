"""Taps on a card's buttons: what the user tapped on a conduit_show card
whose button says ``tap``, kept on the host until Hermes hears it.

Conduit sends a tap before it sends the user's message about it (like
"Send · Email to Mei"): the chat's ids (a chat is known by more than one),
the card as the user saw it, the button's id and the card's field values.
The plugin's pre_llm_call hook hands the tap to that message's turn
(``note``), once, with the card's Markdown, so Hermes knows exactly what the
user said yes to: a tap is the user's OK for what the card showed, nothing
more (Eric, 2026-11-07, issue 1946). Hermes then does it with its own tools
and shows the card again under its live name with the result.

The dashboard loads this module through the plugin's package.
"""

from __future__ import annotations

import json
import re
import os
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from . import card_schema, review_store
from .checklist_store import _exclusive, _sync_directory

FILE_NAME = "conduit-card-taps.json"
MAX_TAPS = 100
MAX_SESSIONS_PER_WRITE = 4
MAX_SESSION_CHARS = 200
MAX_VALUE_CHARS = 2000
# What Hermes reads of the card the user saw.
MAX_CARD_MARKDOWN_CHARS = 3000
# A review's marks: the button-less tap a review card sends (plugin 0.143+).
REVIEW_ACTION = "review"
# A tap Hermes hasn't heard in this long went with a message that never
# reached it: it's forgotten rather than handed to a later turn.
KEEP_S = 15 * 60

_lock = threading.Lock()


class TapError(ValueError):
    pass


def _clean(text: Any, limit: int) -> str:
    if not isinstance(text, str):
        return ""
    return card_schema.clean_text(text)[:limit].rstrip()


def _field_text(text: Any) -> str:
    """A text field's value as the user wrote it: its lines kept (an email's
    paragraphs), each cleaned, and a cut past the cap marked."""
    if not isinstance(text, str):
        return ""
    lines = [card_schema.clean_text(line) for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    joined = "\n".join(lines).strip()
    if len(joined) > MAX_VALUE_CHARS:
        joined = joined[:MAX_VALUE_CHARS].rstrip() + f" … (cut at {MAX_VALUE_CHARS:,} characters)"
    return joined


def _sessions(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise TapError("sessions is a list of the chat's ids")
    sessions = [_clean(item, MAX_SESSION_CHARS) for item in value[:MAX_SESSIONS_PER_WRITE]]
    sessions = list(dict.fromkeys(item for item in sessions if item))
    if not sessions:
        raise TapError("sessions needs the chat's id")
    return sessions


def _tapped(card: dict[str, Any], action: str) -> dict[str, Any] | None:
    """The card's tap button with this id."""
    buttons = [*card.get("actions", []), *(element for section in card.get("sections", [])
                                         for element in section["items"] if element["kind"] == "button")]
    return next((button for button in buttons if button["action"] == "tap" and button["id"] == action), None)


def _fields(card: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {element["id"]: element for section in card.get("sections", []) for element in section["items"]
            if element["kind"] == "field"}


def _values(value: Any, fields: dict[str, dict[str, Any]]) -> dict[str, str]:
    """The field values the tap carried, for the card's own fields only."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TapError("values maps each field's id to what the user left in it")
    values = {}
    for key, field in fields.items():
        raw = value.get(key)
        if isinstance(raw, bool):
            raw = "on" if raw else "off"
        elif isinstance(raw, (int, float)):
            raw = card_schema._cell(raw)
        text = _field_text(raw) if field["input"] == "text" else _clean(raw, MAX_VALUE_CHARS)
        if text and field["input"] in ("choice", "toggle", "date", "number"):
            # Only what the field could hold: an option, on or off, a date, a number within its bounds.
            text = card_schema._field_value({**field, "value": text}) or ""
        values[key] = text
    return values


def _review_label(decisions: list[dict[str, Any]]) -> str:
    """The words the user's message about a review's marks starts with: a
    quick pick (one decision) says what they picked."""
    return "Picked" if len(decisions) == 1 else "Sent review"


def _review_marks(value: Any, decisions: list[dict[str, Any]]) -> dict[str, Any]:
    """The marks a review tap carried, checked against the round as the host
    keeps it: each decision's mark (approve, change or skip), the option it
    names (an approval needs one) and its note, and a note on the round."""
    if not isinstance(value, dict):
        raise TapError("review holds the round and the user's marks")
    given = value.get("marks")
    if not isinstance(given, list) or not given:
        raise TapError("review.marks lists a mark for each decision the user marked")
    by_id = {decision["id"]: decision for decision in decisions}
    marks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in given[:len(decisions)]:
        if not isinstance(raw, dict):
            raise TapError("each mark is an object")
        decision = by_id.get(_clean(raw.get("decision"), 40))
        if decision is None or decision["id"] in seen:
            raise TapError("a mark names none of the round's decisions, or one twice")
        mark = raw.get("mark")
        if mark not in review_store.MARKS:
            raise TapError("a mark is approve, change or skip")
        entry: dict[str, Any] = {"decision": decision["id"], "mark": mark}
        option = _clean(raw.get("option"), 40)
        if option and mark != "skip":
            if not any(choice["id"] == option for choice in decision["options"]):
                raise TapError("a mark names none of its decision's options")
            entry["option"] = option
        elif mark == "approve":
            raise TapError("an approval names the option approved")
        note = _field_text(raw.get("note"))
        if note:
            entry["note"] = note
        seen.add(decision["id"])
        marks.append(entry)
    note = _field_text(value.get("note"))
    return {"decisions": marks, **({"note": note} if note else {})}


def _review_lines(decisions: list[dict[str, Any]], marks: dict[str, Any]) -> str:
    """The marks as Hermes reads them: a line per decision, then the note."""
    by_id = {decision["id"]: decision for decision in decisions}
    lines = []
    for entry in marks["decisions"]:
        decision = by_id[entry["decision"]]
        option = next((choice for choice in decision["options"] if choice["id"] == entry.get("option")), None)
        named = f'{option["id"]} · {option["label"]}' if option else ""
        said = {"approve": f"approved {named}", "change": f"wants changes to {named}" if named else "wants changes",
                "skip": "skipped"}[entry["mark"]]
        line = f'- {decision["title"]}: {said}'
        if entry.get("note"):
            line += f" (note: {json.dumps(entry['note'], ensure_ascii=False)})"
        lines.append(line)
    marked = {entry["decision"] for entry in marks["decisions"]}
    lines += [f'- {decision["title"]}: not marked' for decision in decisions if decision["id"] not in marked]
    if marks.get("note"):
        lines.append(f"Their note on the round: {json.dumps(marks['note'], ensure_ascii=False)}")
    return "\n".join(lines)


def _settles(marks: dict[str, Any], decisions: list[dict[str, Any]]) -> bool:
    """Whether the marks leave nothing to change: every decision approved
    or skipped, and no note."""
    return (not marks.get("note") and len(marks["decisions"]) == len(decisions)
            and all(entry["mark"] in ("approve", "skip") and not entry.get("note") for entry in marks["decisions"]))


class TapStore:
    def __init__(self, home: Any, clock: Callable[[], float] = time.time,
                 reviews: review_store.KeptReviews | None = None):
        self.path = Path(home) / FILE_NAME
        self.clock = clock
        self.home = Path(home)
        self._reviews = reviews

    def reviews(self) -> review_store.KeptReviews:
        """Where reviews are kept: the root home, which every profile shares."""
        if self._reviews is None:
            from . import room_identity
            self._reviews = review_store.KeptReviews(room_identity.root_home(self.home))
        return self._reviews

    def _load(self) -> dict[str, Any]:
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {"taps": []}
        if not isinstance(state, dict) or not isinstance(state.get("taps"), list):
            return {"taps": []}
        now = self.clock()
        kept = [tap for tap in state["taps"] if _well_formed(tap) and now - tap["at"] < KEEP_S]
        # A review tap no turn heard in time: its marks go too (after the
        # save, in _locked), so the user can send them again.
        self._expired = len(kept) != len(state["taps"])
        self._unheard_reviews = {(tap["review"]["id"], tap["review"]["round"]) for tap in state["taps"]
                                 if _well_formed(tap) and now - tap["at"] >= KEEP_S and not tap["heard"]
                                 and isinstance(tap.get("review"), dict)
                                 and isinstance(tap["review"].get("id"), str)
                                 and isinstance(tap["review"].get("round"), int)}
        return {"taps": kept}

    @contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        """Load, let the caller change, then save, under a lock file: the
        dashboard saves taps while the agent hears them, in another process."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(f".{self.path.name}.lock")
        with _lock, open(lock_path, "a+b") as handle, _exclusive(handle):
            try:
                os.chmod(lock_path, 0o600)
            except OSError:
                pass
            self._expired, self._unheard_reviews = False, set()
            state = self._load()
            expired, unheard = self._expired, self._unheard_reviews
            before = json.dumps(state, sort_keys=True)
            yield state
            if expired or json.dumps(state, sort_keys=True) != before:
                self._save(state)
        # Once the expired taps are gone from the file, so it happens once.
        for review_id, number in unheard:
            try:
                self.reviews().drop_marks(review_id, number)
            except (review_store.ReviewError, OSError):
                pass

    def _save(self, state: dict[str, Any]) -> None:
        handle, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".conduit-card-taps-")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                json.dump(state, file, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
            _sync_directory(self.path.parent)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def save(self, body: Any) -> dict[str, Any]:
        """A tap as Conduit sends it, checked against the card it came from;
        answers the tap's id."""
        if not isinstance(body, dict):
            raise TapError("Expected a JSON object")
        sessions = _sessions(body.get("sessions"))
        cards = card_schema.normalize_cards([body.get("card")])
        if not cards:
            raise TapError("card is the card the user tapped, as the chat holds it")
        card = cards[0]
        action = _clean(body.get("action"), 40)
        if card["type"] == "review":
            if action != REVIEW_ACTION:
                raise TapError("a review card sends its marks with action review")
            return self._save_review(sessions, card, body.get("review"))
        button = _tapped(card, action)
        if button is None:
            raise TapError("action names none of the card's tap buttons")
        values = _values(body.get("values"), _fields(card))
        tap = {"id": uuid.uuid4().hex, "at": self.clock(), "heard": False, "action": action,
               "label": button["label"], "title": card.get("title", ""), "live": card.get("live", ""),
               "markdown": _card_markdown(card), "values": values}
        with self._locked() as state:
            for session in sessions:
                state["taps"].append({**tap, "session": session})
            state["taps"] = state["taps"][-MAX_TAPS:]
        return {"ok": True, "id": tap["id"]}

    def _save_review(self, sessions: list[str], card: dict[str, Any], value: Any) -> dict[str, Any]:
        """A review's marks: checked against the round the host keeps, kept
        with it (the next round lists what they settled), and heard as a tap
        by the message about them."""
        shown = card["review"]
        store = self.reviews()
        with self._locked():
            pass  # marks an expired tap left are given back first
        try:
            meta = store.get(shown["id"])
        except review_store.ReviewError as error:
            raise TapError(str(error))
        if meta is None or not 1 <= shown["round"] <= len(meta["rounds"]):
            raise TapError("that review is gone from the host")
        if shown["round"] != len(meta["rounds"]):
            raise TapError("a newer round of that review is in the chat; mark that one")
        decisions = meta["rounds"][shown["round"] - 1]["decisions"]
        marks = _review_marks(value, decisions)
        try:
            store.save_marks(shown["id"], shown["round"], marks, sessions)
        except review_store.ReviewError as error:
            raise TapError(str(error))
        title = meta["title"] or card.get("title", "")
        # The words the app's message starts with, in the user's language
        # ("Picked", "Sent review" by default).
        label = _clean(value.get("label"), 40) or _review_label(decisions)
        # A quick pick's message names the option ("Picked B · Line"), so two
        # quick picks open in one chat are told apart by it.
        picked = ""
        if len(decisions) == 1 and marks["decisions"] and marks["decisions"][0]["mark"] == "approve":
            option = next((option for option in decisions[0]["options"]
                           if option["id"] == marks["decisions"][0].get("option")), None)
            if option is not None:
                picked = f'{option["id"]} · {option["label"]}'
        tap = {"id": uuid.uuid4().hex, "at": self.clock(), "heard": False, "action": REVIEW_ACTION,
               "label": label, "title": title, "live": card.get("live", ""),
               "markdown": _review_lines(decisions, marks), "values": {},
               "review": {"id": shown["id"], "round": shown["round"], "for": meta["for"],
                          "quick": len(decisions) == 1, "settles": _settles(marks, decisions),
                          **({"picked": picked} if picked else {})}}
        try:
            with self._locked() as state:
                for session in sessions:
                    state["taps"].append({**tap, "session": session})
                state["taps"] = state["taps"][-MAX_TAPS:]
        except BaseException:
            # No tap, no marks: the app's retry can send them again.
            store.drop_marks(shown["id"], shown["round"])
            raise
        return {"ok": True, "id": tap["id"]}

    def drop(self, tap_id: Any) -> dict[str, Any]:
        """Forgets a tap Hermes hasn't heard: Conduit posted it, then didn't
        send the message about it (the chat changed, or the user typed in the
        composer meanwhile), so no later message that happens to start with
        its label hears it."""
        tap_id = _clean(tap_id, 64)
        if not tap_id:
            raise TapError("id is the tap's id, as posting it answered")
        with self._locked() as state:
            before = len(state["taps"])
            gone = [tap for tap in state["taps"] if tap["id"] == tap_id and not tap["heard"]]
            state["taps"] = [tap for tap in state["taps"] if tap["id"] != tap_id or tap["heard"]]
            dropped = before - len(state["taps"])
        # A review's marks go with their tap, so the user can send them again.
        review = next((tap["review"] for tap in gone if isinstance(tap.get("review"), dict)), None)
        if review is not None:
            try:
                self.reviews().drop_marks(review["id"], review["round"])
            except review_store.ReviewError:
                pass
        return {"ok": True, "dropped": dropped > 0}

    def note(self, session_id: Any, message: Any,
             forward: Callable[[str, str], dict[str, Any]] | None = None) -> str | None:
        """The taps in this chat Hermes hasn't heard yet, once, for the turn
        of the user's message about them (it starts with the button's label):
        another turn in the chat meanwhile (a cron report, a workstream's)
        leaves them for that message. ``forward(ref, text)`` sends a review's
        marks to the workstream it belongs to (work_tool.forward_review), so
        every round of it goes back to the same workstream."""
        session = _clean(session_id, MAX_SESSION_CHARS)
        if not session:
            return None
        if isinstance(message, str) and message.strip():
            said = message.strip()
        else:
            # A turn with no message of the user's (a cron or workstream turn)
            # leaves the taps for the message about them.
            return None
        with self._locked() as state:
            taps = [tap for tap in state["taps"] if tap["session"] == session and not tap["heard"]
                    and said.startswith(tap["label"])]
            if taps:
                # One message is one tap: first one on the card the message names
                # ("Go · Card A" hears Card A's Go, not a later Go on Card B), then
                # the longest label it starts with, so "Send to all" never hears a
                # waiting "Send" too, then the latest.
                def named(tap: dict[str, Any]) -> bool:
                    picked = tap.get("review", {}).get("picked") if isinstance(tap.get("review"), dict) else None
                    if isinstance(picked, str) and picked and said.startswith(f'{tap["label"]} {picked}'):
                        return True
                    return bool(tap["title"]) and said.startswith(f'{tap["label"]} · {tap["title"]}')
                taps = [max(taps, key=lambda tap: (named(tap), len(tap["label"]), tap["at"]))]
            heard = {tap["id"] for tap in taps}
            # A tap kept under each of the chat's ids is heard under them all.
            for tap in state["taps"]:
                if tap["id"] in heard:
                    tap["heard"] = True
        return "\n".join(_review_line(tap, forward) if isinstance(tap.get("review"), dict) else _line(tap)
                         for tap in taps) or None


def _open_fence(text: str) -> str | None:
    return card_schema.open_fence(text)  # one reading for the tap note and the answer


def _card_markdown(card: dict[str, Any]) -> str:
    """The card's Markdown for the tap's note: cut at a line, a code block
    the cut lands in closed, and folded sections said to be folded."""
    text = card_schema.fallback_markdown([card])
    folded = ("\n(Part of this card was folded under Show more, so they may not have opened it; its fields "
              "went with their starting values unless they changed them.)") \
        if any(section.get("more") for section in card.get("sections", [])) else ""
    # The folded note counts against the cap too.
    room = MAX_CARD_MARKDOWN_CHARS - len(folded)
    if len(text) > room:
        # Room for the longest fence the card could leave open, as clip_markdown does.
        longest = max((len(run) for run in re.findall(r"`{3,}", text)), default=3)
        cut = text[:room - 1 - longest]
        text = cut[:cut.rfind("\n")] if "\n" in cut else cut
        fence = _open_fence(text)
        if fence is not None:
            text += "\n" + fence
    return text + folded


def _line(tap: dict[str, Any]) -> str:
    name = f'"{tap["title"]}"' if tap["title"] else "a card"
    if tap["live"]:
        name += f" (live name {tap['live']})"
    line = (f'[Conduit: the user tapped "{tap["label"]}" (tap {tap["action"]}) on {name} in this chat; their '
            "message says so. The tap is their OK for exactly what the card showed them, nothing more")
    if tap["values"]:
        # A value of several lines goes quoted, its line breaks written \n, so it reads as one value.
        filled = "; ".join(f"{key}: {json.dumps(value, ensure_ascii=False) if chr(10) in value else value}"
                           if value else f"{key}: (empty)" for key, value in tap["values"].items())
        line += f", with the fields as they left them ({filled})"
    again = ("show the card again with the same live name" if tap["live"]
             else "show the card again (it had no live name, so give the new one a live name from now on)")
    line += (f". Do it now with your tools, then {again}, saying how it went "
             "(done, or what went wrong). The card they saw:\n" + tap["markdown"] + "]")
    return line


def _forwarded_text(tap: dict[str, Any]) -> str:
    """What a review's workstream gets: the marks, and what to do next."""
    review = tap["review"]
    then = ("Every decision is settled: carry on with what they approved, and report when it's done."
            if review["settles"] else
            "Build the next round for what they marked to change (and what their notes ask), keeping what they "
            "approved and leaving out what they skipped. Write each option's page to a file and report the files: "
            "the Bot Chat shows them to the user as the next round of the same review.")
    return (f'The user marked round {review["round"]} of your review "{tap["title"]}" ({review["id"]}):\n'
            f'{tap["markdown"]}\n{then}')


def _review_line(tap: dict[str, Any], forward: Callable[[str, str], dict[str, Any]] | None) -> str:
    review = tap["review"]
    name = f'"{tap["title"]}"' if tap["title"] else "a review"
    head = (f"[Conduit: the user sent their marks on round {review['round']} of the review {name} "
            f"({review['id']}) in this chat; their message says so. Their marks:\n{tap['markdown']}\n")
    ref = review["for"]
    if ref:
        sent: dict[str, Any] = {}
        if forward is not None:
            try:
                sent = forward(ref, _forwarded_text(tap)) or {}
            except Exception:  # noqa: BLE001 — the note says to send them instead
                sent = {}
        if sent.get("ok"):
            return head + (f"These marks already went to {ref}, which made the options: don't send them again or "
                           f"redo its work. Say in a line that {ref} has them. When {ref} reports its next round, "
                           f"show it with conduit_show, review id {review['id']} and for {ref}.]")
        why = f" ({sent['note']})" if isinstance(sent.get("note"), str) and sent["note"] else ""
        return head + (f"They belong to {ref}, which made the options, and couldn't be passed on to it for you{why}. "
                       f"Send them to {ref} with work_send now, then show its next round with review id "
                       f"{review['id']} and for {ref}.]")
    if review["settles"]:
        what = "They picked" if review["quick"] else "Every decision is settled"
        return head + f"{what}: go ahead with what they approved now, with your tools.]"
    return head + ("Build the next round for what they marked to change (and what their notes ask), keeping what "
                   "they approved; decisions they approved or skipped needn't be shown again. Then show it with "
                   f"conduit_show, review id {review['id']}, so it stays the same review.]")


def _well_formed(tap: Any) -> bool:
    return (isinstance(tap, dict) and all(isinstance(tap.get(key), str) for key in
                                          ("id", "session", "action", "label", "title", "live", "markdown"))
            and isinstance(tap.get("at"), (int, float)) and not isinstance(tap.get("at"), bool)
            and isinstance(tap.get("heard"), bool) and isinstance(tap.get("values"), dict))
