"""Pulse's picks: what each card says, as structured data the app draws.

Every model call that makes a Pulse card (the report's candidate list, the
look ahead, a follow-up's check, the morning brief) gives each thing it
brings up as one pick:

    {"line": "...", "detail": "...", "subject": "#41",
     "incident": {"card": "new"},
     "shape": "runs", "facts": {...}, "actions": [...]}

``line`` and ``detail`` are words for the user; the card's ``news`` is them
both, so an app from before looks (and every check on a card's words) reads
what it always did. ``shape`` and ``facts`` say how the app draws it: one of
SHAPES, its facts checked here, and a pick whose facts don't hold up draws
as "plain" (its words are never parsed for them). ``actions`` are the
card's buttons, from ACTIONS: nothing a button does runs before the user
sees the words (an "ask" leaves them in the chat's composer to edit).

A pick's subject is the thing it's about (a remembered item, a reminder, a
calendar entry, a scheduled job); its incident is this particular finding
about it: a new card, an update to an open card, or the close of one (Eric,
2026-11-09: the audit cron failing twice, recovering, then failing again
three days later is one card updated, then closed, then a new card).
Cards are named by their permanent id, "pc_<id>", never by where they sit
in a list.
"""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, timezone
from typing import Any, Collection, Iterable, Mapping, Optional, Sequence

from . import card_schema

PLAIN = "plain"
DRAWN = "drawn"
NUMBER = "number"
WAITING = "waiting"
FOUND = "found"
# Shapes a pick may take only once every Conduit that draws cards draws
# them (MORE_SHAPES_FEATURE, through drawn_types): an older app would
# show them as plain words, which is what Pulse leaves out (Eric,
# 2026-11-11: a number that changed, someone the user is waiting on, and
# what a watch found, for what fit no card type; three more at most).
GATED_SHAPES = (NUMBER, WAITING, FOUND)
SHAPES = ("date", "due", "runs", "promise", "status", "steps", NUMBER, WAITING, FOUND, DRAWN, PLAIN)
# The morning brief's own look (pulse_brief.look): made in code from its
# reply, never a shape the model picks.
BRIEF = "brief"
ASK, OPEN, DRAFT, DONE, LATER = "ask", "open", "draft", "done", "later"
ACTIONS = (ASK, OPEN, DRAFT, DONE, LATER)
MAX_ACTIONS = 2
LINE_LIMIT = 200
DETAIL_LIMIT = 400
LABEL_LIMIT = 24
SAY_LIMIT = 400
FACT_LIMIT = 120
MAX_RUNS = 10
MAX_STEPS = 6
STATES = ("ok", "warn", "fail")
UNIT_LIMIT = 12
BETTER = ("up", "down")
RUN_RESULTS = ("ok", "fail")
# What a drawn body may be: the chat's cards that show something, never a
# question, a form or a live card (a Pulse card's buttons are its actions).
DRAWN_TYPES = card_schema.INFO_TYPES + card_schema.MEDIA_TYPES + card_schema.PLACE_TYPES + (
    "table", "timeline", "checklist", "list", "progress", "callout") + card_schema.COMPOSED_TYPES

CARD_PREFIX = "pc_"
NEW = "new"
UPDATE = "update"
CLOSE = "close"

# The rules every pick-making prompt carries, so a pick reads alike from
# the report, the look ahead, a follow-up and the brief.
PICK_RULES = """Each thing you bring up is a pick:
{{"line": "...", "detail": "...", "subject": "#41", "incident": {{"card": "new"}}, "shape": "plain", "facts": {{}}, "actions": [{{"label": "...", "does": "ask", "say": "..."}}]}}
- "line": the headline, written to the user, at most one sentence, short (under 15 words), saying the thing itself, never a reason or advice the context doesn't give ("so you can plan around it"). "detail": one more sentence that adds what the line doesn't say (what to do, a number, a name), or "", never the line said again. The card draws the shape's facts under them, so neither needs to repeat them.
- "subject": the key of what it's about, as the context gives it: a remembered thing (#41), a reminder (r2), a calendar entry (v1), a scheduled job (j3); "" when it has none.
- "incident": {{"card": "new"}} for something new. When a card under "What Pulse brought up lately" is still open and this is more about that same occurrence (another failed run, a new step), {{"card": "pc_1834"}} with that card's id updates it. When that occurrence is over (the job recovered, it arrived), {{"close": "pc_1834"}} closes it. Something that happened again after it was over is new, not an update.
- "shape" and its "facts", only what the context states, else "plain" with {{}}:
  "date" {{"day": "YYYY-MM-DD", "what": "birthday", "whose": "Kwan"}} ("whose": a person's name, or "" for the user's own); "due" {{"due": "YYYY-MM-DD", "noticed": "YYYY-MM-DD"}} (something to get done by a day; "noticed": the earlier day it first came up, left out when that's the due day); "runs" {{"job": "name", "results": ["ok", "fail"]}} (a scheduled job's recent runs, oldest first, at most 10: use it rather than "status" when the context shows its runs); "promise" {{"person": "Mara", "said": "their words, quoted", "where": "chat title", "when": "Fri"}}; "status" {{"label": "what", "state": "ok" | "warn" | "fail"}} ("label": the thing in two to four words, like "Nightly backup"); "steps" {{"names": ["Ordered", "Shipped", "Out today", "Delivered"], "at": 2}} (at = the current step, counted from 0). Pick the shape the facts support: a next step after something done is "steps" (what's done, then what's next); something the user or Hermes said they'd do, or the user asked to be reminded of, with no day, is "promise" in the words they said; something that went wrong is "status" or "runs". Never make up a fact to fit a shape.
- "actions": at most 2 buttons, each with a short "label" (a few words, like "Restart it") and "does": "ask" with "say" (words for the user to send Hermes in that thing's chat; they can edit them first), "open" with "url" (an https link the context gives), "draft" with "text" (a message for the user to send someone), "done" or "later". Give the one most useful step first, named for what it does ("Verify ID", "Plan the day"), never a bare "Do it". For something that went wrong (a failing job, an error, a "warn" or "fail" status), that first step is an "ask" whose "say" has Hermes find out what's wrong and fix it, never only ask how it stands. A calendar entry is already on the user's calendar, with its own alerts: never offer to add it, block time for it, remind about it or check it again; give a step only for preparation the context names, else just "done". An entry on a holiday calendar for another country ("Holidays in Japan") isn't a day off the user keeps. Every step is one Hermes can actually take: it can't change the Conduit app or what its cards can show. [] when there's nothing to do."""


def card_id_text(card_id: int) -> str:
    return f"{CARD_PREFIX}{int(card_id)}"


def card_id(value: Any) -> Optional[int]:
    """The card a "pc_<id>" names; None for anything else."""
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"\s*pc_(\d{1,12})\s*", value)
    return int(match.group(1)) if match else None


def clip(value: Any, limit: int) -> str:
    """``value`` as one line of words, cut to ``limit`` with an ellipsis."""
    return _text(value, limit)


def _text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(card_schema.clean_text(value).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _day(value: Any, clock: bool = False) -> str:
    """An ISO day ("2026-11-20"), or with ``clock`` a day and time too; ""
    for anything else."""
    if not isinstance(value, str):
        return ""
    value = value.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            date.fromisoformat(value)
            return value
        if clock and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", value):
            datetime.fromisoformat(value)
            return value
    except ValueError:
        return ""
    return ""


# Words in a date's "whose" that name nobody (the user themselves).
NOBODY = frozenset({"", "your", "you", "yours", "my", "me", "mine", "our", "ours", "we", "user", "the user",
                    "their", "theirs", "his", "her", "hers"})
_STOP = frozenset("a an and are as at be both by for from has have in is it its of on or so that the their this "
                  "to was were will with you your".split())


def _stems(text: str) -> list[str]:
    return [word[:5] for word in re.findall(r"[a-z0-9]+", text.lower()) if word not in _STOP]


def repeats(detail: str, line: str) -> bool:
    """Whether ``detail`` mostly says ``line`` again: at least
    REPEAT_SHARE of its words (stop words aside, compared by their first
    five letters) are in the line. One with fewer than SHORT_DETAIL words
    repeats only when every one of them is in the line, so a short detail
    that adds a word ("today", a name) is kept."""
    words, said = _stems(detail), set(_stems(line))
    if not words:
        return False
    # A short detail that adds one word ("today", a name) still says something.
    if len(words) < SHORT_DETAIL:
        return all(word in said for word in words)
    return sum(word in said for word in words) / len(words) >= REPEAT_SHARE


REPEAT_SHARE = 0.6
# Fewer words than this (stop words aside): dropped only when every one is in the line.
SHORT_DETAIL = 5


_PLAIN_NUMBER = re.compile(r"[+-]?(\d+|\d{1,3}(,\d{3})+)(\.\d+)?")


def _number(value: Any) -> Optional[float | int]:
    """A finite number, or a string that's only one ("91", "1,204.5"), as
    an int when it's whole; None otherwise. A comma is read only as a
    thousands mark between groups of three: "1.204,50" could be 1204.5 or
    1.2045, so it's no number here rather than a wrong one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        if not _PLAIN_NUMBER.fullmatch(value.strip()):
            return None
        value = float(value.strip().replace(",", ""))
    if not isinstance(value, (int, float)):
        return None
    try:
        # A whole number too big for a float is no number a card can show.
        if not math.isfinite(float(value)):
            return None
    except OverflowError:
        return None
    return int(value) if float(value).is_integer() and abs(value) < 1e15 else float(value)


def _facts(shape: str, facts: Any, drawn_types: Sequence[str]) -> tuple[Optional[dict], str]:
    """``facts`` checked for ``shape``: (the facts kept, "") or (None, why
    they don't hold up)."""
    if shape == PLAIN:
        return {}, ""
    if shape in GATED_SHAPES and shape not in drawn_types:
        return None, "shape_not_drawn"
    if not isinstance(facts, dict):
        return None, "no_facts"
    if shape == "date":
        day = _day(facts.get("day"))
        if not day:
            return None, "bad_day"
        whose = _text(facts.get("whose"), FACT_LIMIT)
        # "your" or "the user" names nobody: the tile shows only a name.
        return {"day": day, "what": _text(facts.get("what"), FACT_LIMIT),
                "whose": "" if re.sub(r"['’]s$", "", whose.lower().strip(" .")) in NOBODY else whose}, ""
    if shape == "due":
        due = _day(facts.get("due"), clock=True)
        if not due:
            return None, "bad_due"
        noticed = _day(facts.get("noticed"))
        # Noticed the day it's due, a bar from it to the due day says nothing.
        return {"due": due, **({"noticed": noticed} if noticed and noticed < due[:10] else {})}, ""
    if shape == "runs":
        results = facts.get("results")
        if not isinstance(results, list) or not results or any(r not in RUN_RESULTS for r in results):
            return None, "bad_runs"
        return {"job": _text(facts.get("job"), FACT_LIMIT), "results": list(results[-MAX_RUNS:])}, ""
    if shape == "promise":
        said = _text(facts.get("said"), SAY_LIMIT)
        if not said:
            return None, "no_words"
        return {"said": said, **{key: _text(facts.get(key), FACT_LIMIT) for key in ("person", "where", "when")}}, ""
    if shape == "status":
        label, state = _text(facts.get("label"), FACT_LIMIT), facts.get("state")
        if not label or state not in STATES:
            return None, "bad_status"
        return {"label": label, "state": state}, ""
    if shape == "steps":
        names, at = facts.get("names"), facts.get("at")
        if (not isinstance(names, list) or not 2 <= len(names) <= MAX_STEPS
                or not all(isinstance(n, str) and n.strip() for n in names)
                or not isinstance(at, int) or isinstance(at, bool) or not 0 <= at < len(names)):
            return None, "bad_steps"
        return {"names": [_text(n, 40) for n in names], "at": at}, ""
    if shape == NUMBER:
        label, value = _text(facts.get("label"), FACT_LIMIT), _number(facts.get("value"))
        if not label or value is None:
            return None, "bad_number"
        kept: dict[str, Any] = {"label": label, "value": value, "unit": _text(facts.get("unit"), UNIT_LIMIT)}
        for key in ("was", "limit"):
            if (number := _number(facts.get(key))) is not None:
                kept[key] = number
        if facts.get("better") in BETTER:
            kept["better"] = facts["better"]
        return kept, ""
    if shape == WAITING:
        person, what = _text(facts.get("person"), FACT_LIMIT), _text(facts.get("what"), FACT_LIMIT)
        if not person or not what:
            return None, "bad_waiting"
        since = _day(facts.get("since"))
        return {"person": person, "what": what, **({"since": since} if since else {})}, ""
    if shape == FOUND:
        found = {key: _text(facts.get(key), FACT_LIMIT) for key in ("asked", "answer", "where")}
        if not all(found.values()):
            return None, "bad_found"
        return found, ""
    if shape == DRAWN:
        body = facts.get("body")
        kind = body.get("type") if isinstance(body, dict) else None
        if kind not in DRAWN_TYPES or kind not in drawn_types:
            return None, "body_type"
        card = card_schema.normalize_card(body)
        card = card_schema.without_actions(card) if card is not None else None
        if card is None:
            return None, "bad_body"
        card.pop("live", None)
        return {"body": card}, ""
    return None, "unknown_shape"


def _url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    url = value.strip()
    if (not url or len(url) > 2000 or not url.startswith(card_schema.OPEN_SCHEMES)
            or card_schema._OPEN_BANNED.search(url)):
        return ""
    return url


# A calendar entry's key as the model gives it (pulse_report.phone_context)
# and as a card keeps it (pulse_candidates.EVENT), and the button words
# that would put it on, or check it against, the calendar it came from.
# Moving or cancelling it is a real step and stays.
_CALENDAR_SUBJECT = re.compile(r"v\d+")
CALENDAR_CARD_SUBJECT = "event:"
_ONTO_CALENDAR = re.compile(r"\b(?:calendar|schedule)\b|\bblock\b.*\btime\b", re.IGNORECASE)
# A reminder for the user (the entry has its own alerts), never one for
# someone else, such as a draft "Remind Mara to confirm". A keep-word in it
# ("Remind me to reschedule it") is still a reminder.
_REMINDER = re.compile(r"\bremind(?:ed|ing)? (?:me|us)\b|\breminders?\b", re.IGNORECASE)
_KEEPS_STEP = re.compile(r"\b(?:reschedul\w*|cancel\w*|mov(?:e|es|ed|ing)|shift\w*)\b", re.IGNORECASE)


def _onto_calendar(action: Mapping[str, Any]) -> bool:
    if action.get("does") == OPEN:  # a link to the entry only shows it
        return False
    # A draft's text is a message to someone else: their calendar, or a
    # reminder they're asked for, isn't the user's entry.
    words = " ".join(str(action.get(key) or "") for key in ("label", "say"))
    # A draft is a message to someone else: "Send Mara a reminder" is theirs.
    if action.get("does") != DRAFT and _REMINDER.search(words):
        return True
    return bool(_ONTO_CALENDAR.search(words)) and not _KEEPS_STEP.search(words)


def from_calendar(subject: Any, event: Any = "") -> bool:
    """Whether a pick or card is about a calendar entry: by the model's key
    ("v1", as its subject or event) or by a card's "event:" subject."""
    for key in (subject, event):
        key = str(key or "").strip().lower()
        if _CALENDAR_SUBJECT.fullmatch(key) or key.startswith(CALENDAR_CARD_SUBJECT):
            return True
    return False


def off_calendar(actions: Any) -> list:
    """``actions`` without a button to put a calendar entry on the calendar
    it was read from."""
    return [action for action in actions if not (isinstance(action, Mapping) and _onto_calendar(action))] \
        if isinstance(actions, list) else []


def _actions(value: Any) -> list[dict]:
    """The buttons that hold up, at most MAX_ACTIONS, one each of what they do."""
    out: list[dict] = []
    if not isinstance(value, list):
        return out
    for raw in value:
        if len(out) == MAX_ACTIONS:
            break
        if not isinstance(raw, dict) or raw.get("does") not in ACTIONS:
            continue
        does = raw["does"]
        label = _text(raw.get("label"), LABEL_LIMIT)
        if not label or any(action["does"] == does for action in out):
            continue
        action: dict[str, Any] = {"label": label, "does": does}
        if does == ASK:
            action["say"] = _text(raw.get("say"), SAY_LIMIT)
        elif does == OPEN:
            action["url"] = _url(raw.get("url"))
        elif does == DRAFT:
            action["text"] = _text(raw.get("text"), SAY_LIMIT)
        if any(value == "" for value in action.values()):
            continue
        out.append(action)
    return out


def incident(value: Any) -> tuple[str, Optional[int]]:
    """What a pick does with a card: (NEW, None), (UPDATE, id) or (CLOSE, id)."""
    if isinstance(value, dict):
        closed = card_id(value.get("close"))
        if closed is not None:
            return CLOSE, closed
        updated = card_id(value.get("card"))
        if updated is not None:
            return UPDATE, updated
    return NEW, None


def parse(entry: Any, *, drawn_types: Sequence[str] = (), line: str = "") -> tuple[Optional[dict], str]:
    """One pick from a model's entry: (the pick, "") with every part checked,
    shape "plain" and the reason when its facts don't hold up; (None, why)
    when it has no words. ``line``: the words the caller already has for it
    (the candidate list's own "line"), used when the entry has none."""
    if not isinstance(entry, dict):
        return None, "not_an_object"
    words = _text(entry.get("line"), LINE_LIMIT) or _text(line, LINE_LIMIT)
    if not words:
        return None, "no_line"
    shape = entry.get("shape") if entry.get("shape") in SHAPES else PLAIN
    facts, why = _facts(shape, entry.get("facts"), drawn_types)
    if facts is None:
        shape, facts = PLAIN, {}
    elif entry.get("shape") not in (None, "") and entry.get("shape") not in SHAPES:
        why = "unknown_shape"
    kind, card = incident(entry.get("incident"))
    pick: dict[str, Any] = {
        "line": words, "detail": "" if repeats(detail := _text(entry.get("detail"), DETAIL_LIMIT), words) else detail,
        "shape": shape, "facts": facts,
        # A calendar entry's buttons lose the calendar ones before the cap, so
        # a step after them still fits.
        "actions": _actions(off_calendar(entry.get("actions")) if from_calendar(entry.get("subject"), entry.get("event"))
                            else entry.get("actions")), "incident": {"card": NEW} if kind == NEW else
        ({"card": card_id_text(card)} if kind == UPDATE else {"close": card_id_text(card)})}
    subject = entry.get("subject")
    if isinstance(subject, str) and subject.strip():
        pick["subject"] = subject.strip()[:40]
    return pick, why


def news(pick: Mapping[str, Any]) -> str:
    """A pick's words as one card text: its line, then its detail."""
    line, detail = str(pick.get("line") or ""), str(pick.get("detail") or "")
    if not detail:
        return line
    return f"{line}{'' if line[-1:] in '.!?…' else '.'} {detail}"


def merged_facts(shape: str, old: Mapping[str, Any], new: Mapping[str, Any]) -> dict:
    """An update's facts on top of the card's: a run history grows (the
    newer results after what the card had, at most MAX_RUNS), anything else
    takes the newer facts. A number's "was" goes with the value it was
    given for: a newer value that says nothing of what it was before
    doesn't keep the older card's, and it keeps the card's unit when it
    names none."""
    if shape == "runs" and isinstance(old.get("results"), list) and isinstance(new.get("results"), list):
        mine, theirs = list(old["results"]), list(new["results"])
        # The model sees the whole recent history each time: what it gives
        # that the card's history already ends with isn't new.
        overlap = 0
        for size in range(min(len(mine), len(theirs)), 0, -1):
            if mine[-size:] == theirs[:size]:
                overlap = size
                break
        return {**old, **new, "results": (mine + theirs[overlap:])[-MAX_RUNS:]}
    if shape == NUMBER:
        if "value" in new and "was" not in new:
            old = {key: value for key, value in old.items() if key != "was"}
        # The same thing keeps its unit when the newer facts name none.
        if not new.get("unit") and old.get("unit"):
            new = {key: value for key, value in new.items() if key != "unit"}
    return {**old, **new}


def dumps(pick: Optional[Mapping[str, Any]]) -> str:
    return json.dumps(pick, ensure_ascii=False, separators=(",", ":"), sort_keys=True) if pick else ""


def loads(text: Any) -> Optional[dict]:
    if not isinstance(text, str) or not text:
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def look(card: Mapping[str, Any]) -> dict:
    """What the app draws for a card: its stored pick's shape, facts and
    actions, or "plain" for a card from before picks (or a pick that
    didn't keep)."""
    pick = loads(card.get("look"))
    if pick is None:
        return {"shape": PLAIN, "facts": {}, "actions": []}
    actions = pick.get("actions") if isinstance(pick.get("actions"), list) else []
    return {"shape": pick.get("shape") if pick.get("shape") in SHAPES + (BRIEF,) else PLAIN,
            "facts": pick.get("facts") if isinstance(pick.get("facts"), dict) else {},
            # A card kept from before calendar entries lost the button loses it too.
            "actions": off_calendar(actions) if from_calendar(card.get("subject"), pick.get("subject")) else actions,
            "line": str(pick.get("line") or ""), "detail": str(pick.get("detail") or "")}


# A "new" incident about a subject with a card open from this recently is
# that card's, not another (it's updated instead).
HOLD_S = 86400.0
HOLD = "hold"
DROP = "drop"
CARD_KINDS = ("followup", "noticed")
# A remembered item's subject (pulse_candidates.subject_of).
ITEM_SUBJECT = "item:"
# A pick decide let past the hold because it is due today (pulse_candidates).
DUE_AGAIN = "due_again"
# A yearly date's or a deadline's subject (pulse_dates keys): it comes up at
# each of its lead times on purpose, each once by its ref, so a card open
# from the last lead time doesn't hold the next one back.
DATE_SUBJECTS = ("person:", "fact:", "deadline:")


def same_subject(card_subject: str, subject: str) -> bool:
    """Whether a pick about ``subject`` may change a card about
    ``card_subject`` it names by id: either has none, they're the same, or
    one is a look ahead's date (person:, fact:, deadline:) and the other a
    remembered item (item:), which a report keys the same day by: the id
    named wins there. Otherwise two subjects are two things."""
    if not card_subject or not subject or card_subject == subject:
        return True
    pair = {card_subject.startswith(DATE_SUBJECTS), subject.startswith(DATE_SUBJECTS)}
    return pair == {True, False} and ITEM_SUBJECT in (card_subject[:len(ITEM_SUBJECT)], subject[:len(ITEM_SUBJECT)])


def resolve(pulse_store: Any, pick: Mapping[str, Any], subject: str, now: float,
            closing: Collection[int] = ()) -> tuple[str, Optional[int], str]:
    """What a pick does, checked against the store: (NEW, None, why),
    (UPDATE, card, ""), (CLOSE, card, "") or (DROP, card or None, why). An
    update to a card that's gone, closed or about something else is a new
    card; a close of one is nothing; a new incident about a ``subject`` that
    has a card open from the last HOLD_S is held for that card (DROP, HOLD):
    only an update by the card's id changes it. ``closing``: cards a pick
    in the same batch closes (``closed_in``), which hold nothing back (a job
    that recovered and failed again since the last report) and take no
    update."""
    kind, target = incident(pick.get("incident"))
    if target is not None:
        card = pulse_store.card(target)
        live = card is not None and card["kind"] in CARD_KINDS and pulse_store.card_open(target)
        mine = live and same_subject(str(card.get("subject") or ""), subject)
        if kind == CLOSE:
            return (CLOSE, target, "") if mine else (DROP, None, "close_subject" if live else "close_unknown")
        if mine and target not in closing:
            return UPDATE, target, ""
        kind = NEW
        why = ("update_unknown" if card is None or card["kind"] not in CARD_KINDS
               else "update_closed" if target in closing or not pulse_store.card_open(target)
               else "update_subject")
    else:
        why = ""
    # One due today (pulse_candidates.due_again, marked by decide with the
    # last word of the card it was held for) comes once more past the cards
    # from before then, so a deadline can't slip by behind them; one carded
    # since (its repeat already said) still holds it.
    said_by = pick.get(DUE_AGAIN)
    said_by = float(said_by) if isinstance(said_by, (int, float)) and not isinstance(said_by, bool) else None
    if subject and not subject.startswith(DATE_SUBJECTS):
        last = lambda card: max(float(card["created_at"]), float(card.get("updated_at") or 0))
        open_now = [card for card in pulse_store.open_cards_with([subject], now - HOLD_S)
                    if int(card["id"]) not in closing and (said_by is None or last(card) > said_by)]
        if open_now:
            # Its card says it still: no second card (Pulse's open-card rule).
            return DROP, int(open_now[0]["id"]), HOLD
    return NEW, None, why


def closed_in(pulse_store: Any, entries: Iterable[tuple[Optional[Mapping[str, Any]], str]],
              now: float) -> list[int]:
    """The open cards a batch's picks close (each entry a pick, or None, and
    its subject), found before any pick is resolved, so a new incident about
    the same subject isn't held by a card the batch closes, wherever in it
    the close comes."""
    out: list[int] = []
    for pick, subject in entries:
        if pick is None or incident(pick.get("incident"))[0] != CLOSE:
            continue
        action, target, _ = resolve(pulse_store, pick, subject, now)
        if action == CLOSE and target is not None and target not in out:
            out.append(target)
    return out


def updated_card(card: Mapping[str, Any], pick: Mapping[str, Any], subject: str = "") -> dict[str, Any]:
    """The columns an update rewrites on ``card``: its words and its look,
    the facts built up on the card's when the shape stays, and ``subject``
    when the card had none (one from before picks), so a later pick about it
    is held for it."""
    old = loads(card.get("look")) or {}
    merged = dict(pick)
    if old.get("shape") == pick.get("shape") and isinstance(old.get("facts"), dict):
        merged["facts"] = merged_facts(str(pick.get("shape")), old["facts"], pick.get("facts") or {})
    if not merged.get("actions") and isinstance(old.get("actions"), list):
        merged["actions"] = old["actions"]
    if from_calendar(subject or card.get("subject"), pick.get("subject")):
        merged["actions"] = off_calendar(merged.get("actions"))
    merged["incident"] = {"card": card_id_text(int(card["id"]))}
    columns = {"news": news(merged), "look": dumps(merged)}
    if subject and not card.get("subject"):
        columns["subject"] = subject
    return columns


# Counting (a day's picks, kept in Pulse's store for `pulse picks`) ----------

PICK_COUNTS = "pick_counts"
COUNTS_KEPT_DAYS = 14


def count(pulse_store: Any, now: float, keys: Sequence[str]) -> None:
    """Adds one to each of ``keys`` for the UTC day of ``now``, keeping the
    last COUNTS_KEPT_DAYS days; best effort."""
    if not keys:
        return
    try:
        day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
        pulse_store.bump_counts(PICK_COUNTS, day, list(keys), keep_days=COUNTS_KEPT_DAYS)
    except Exception:  # noqa: BLE001 - counting never stops a card
        pass


# What a Conduit that draws Pulse cards from their picks says it can do.
LOOKS_FEATURE = "pulse-looks"


# What a Conduit that draws GATED_SHAPES says (2.4.x).
MORE_SHAPES_FEATURE = "pulse-more-shapes"


def drawn_types(home: Any) -> tuple[str, ...]:
    """What every Conduit that draws cards can draw beyond the first shapes:
    the chat card types a "drawn" pick may use, then GATED_SHAPES. None
    until every one draws Pulse looks; then the card types whose feature
    every one of them has (as conduit_show leaves out what one can't draw),
    and the gated shapes once every one says MORE_SHAPES_FEATURE."""
    try:
        from .present_store import PresentStore
        present = PresentStore(home)
        if not present.has_feature_everywhere(LOOKS_FEATURE):
            return ()
        hidden = {kind for feature, kinds in card_schema.FEATURE_TYPES.items()
                  if not present.has_feature_everywhere(feature) for kind in kinds}
        shapes = GATED_SHAPES if present.has_feature_everywhere(MORE_SHAPES_FEATURE) else ()
    except Exception:  # noqa: BLE001 - nothing drawn when it can't tell
        return ()
    return tuple(kind for kind in DRAWN_TYPES if kind not in hidden) + shapes


def rules(drawn: Sequence[str]) -> str:
    """PICK_RULES for a prompt, with the "drawn" shape when there are card
    types to draw it with."""
    text = PICK_RULES.replace("{{", "{").replace("}}", "}")
    if NUMBER in drawn:
        text += ("\n- \"number\" {\"label\": \"Disk on nas-01\", \"value\": 91, \"unit\": \"%\", \"was\": 78, \"limit\": 90, "
                 "\"better\": \"down\"}: a measured value the context states, when the news is the number itself (a price, "
                 "a balance, how full something is); \"was\": the earlier value it gives, \"limit\": a threshold it names, "
                 "\"better\": \"up\" or \"down\" when which way is good is plain; leave out what it doesn't give. Use it "
                 "rather than \"status\" when there's a number to show.")
    if WAITING in drawn:
        text += ("\n- \"waiting\" {\"person\": \"Dana\", \"what\": \"the signed lease\", \"since\": \"YYYY-MM-DD\"}: someone "
                 "owes the user a reply or a thing (who, what, and the day they were asked or said they'd send it, "
                 "left out when the context doesn't give it). It's the other side of \"promise\", which is what the user "
                 "or Hermes said they'd do.")
    if FOUND in drawn:
        text += ("\n- \"found\" {\"asked\": \"tickets for the Phoebe Bridgers show\", \"answer\": \"On sale Friday at 10am\", "
                 "\"where\": \"Ticketmaster\"}: the answer to something the user asked Hermes to watch for or find out (what "
                 "they asked for, what was found, and where it was found, each as the context gives it). Use it for a "
                 "watch's or a question's answer that no other shape shows.")
    cards = [kind for kind in drawn if kind not in GATED_SHAPES]
    if cards:
        text += ("\n- \"drawn\" {\"body\": a card} when none of those shapes fits and a chart, weather, steps, stats, "
                 "a table or another card shows it better than words: the body is one conduit_show card of type "
                 + ", ".join(cards) + ", with no buttons.")
    return text
