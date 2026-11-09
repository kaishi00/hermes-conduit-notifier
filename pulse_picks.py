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
import re
from datetime import date, datetime, timezone
from typing import Any, Collection, Iterable, Mapping, Optional, Sequence

from . import card_schema

PLAIN = "plain"
DRAWN = "drawn"
SHAPES = ("date", "due", "runs", "promise", "status", "steps", DRAWN, PLAIN)
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
- "line": the headline, written to the user, at most one sentence. "detail": at most one more sentence, or "".
- "subject": the key of what it's about, as the context gives it: a remembered thing (#41), a reminder (r2), a calendar entry (v1), a scheduled job (j3); "" when it has none.
- "incident": {{"card": "new"}} for something new. When a card under "What Pulse brought up lately" is still open and this is more about that same occurrence (another failed run, a new step), {{"card": "pc_1834"}} with that card's id updates it. When that occurrence is over (the job recovered, it arrived), {{"close": "pc_1834"}} closes it. Something that happened again after it was over is new, not an update.
- "shape" and its "facts", only what the context states, else "plain" with {{}}:
  "date" {{"day": "YYYY-MM-DD", "what": "birthday", "whose": "Kwan"}}; "due" {{"due": "YYYY-MM-DD", "noticed": "YYYY-MM-DD"}} (something to get done by a day); "runs" {{"job": "name", "results": ["ok", "fail"]}} (oldest first, at most 10); "promise" {{"person": "Mara", "said": "their words, quoted", "where": "chat title", "when": "Fri"}}; "status" {{"label": "what", "state": "ok" | "warn" | "fail"}}; "steps" {{"names": ["Ordered", "Shipped", "Out today", "Delivered"], "at": 2}} (at = the current step, counted from 0).
- "actions": at most 2 buttons, each with a short "label" (a few words, like "Restart it") and "does": "ask" with "say" (words for the user to send Hermes in that thing's chat; they can edit them first), "open" with "url" (an https link the context gives), "draft" with "text" (a message for the user to send someone), "done" or "later". [] when there's nothing to do."""


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


def _facts(shape: str, facts: Any, drawn_types: Sequence[str]) -> tuple[Optional[dict], str]:
    """``facts`` checked for ``shape``: (the facts kept, "") or (None, why
    they don't hold up)."""
    if shape == PLAIN:
        return {}, ""
    if not isinstance(facts, dict):
        return None, "no_facts"
    if shape == "date":
        day = _day(facts.get("day"))
        if not day:
            return None, "bad_day"
        return {"day": day, "what": _text(facts.get("what"), FACT_LIMIT),
                "whose": _text(facts.get("whose"), FACT_LIMIT)}, ""
    if shape == "due":
        due = _day(facts.get("due"), clock=True)
        if not due:
            return None, "bad_due"
        noticed = _day(facts.get("noticed"))
        return {"due": due, **({"noticed": noticed} if noticed and noticed <= due[:10] else {})}, ""
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
        "line": words, "detail": _text(entry.get("detail"), DETAIL_LIMIT), "shape": shape, "facts": facts,
        "actions": _actions(entry.get("actions")), "incident": {"card": NEW} if kind == NEW else
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
    takes the newer facts."""
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
    return {"shape": pick.get("shape") if pick.get("shape") in SHAPES + (BRIEF,) else PLAIN,
            "facts": pick.get("facts") if isinstance(pick.get("facts"), dict) else {},
            "actions": pick.get("actions") if isinstance(pick.get("actions"), list) else [],
            "line": str(pick.get("line") or ""), "detail": str(pick.get("detail") or "")}


# A "new" incident about a subject with a card open from this recently is
# that card's, not another (it's updated instead).
HOLD_S = 86400.0
HOLD = "hold"
DROP = "drop"
CARD_KINDS = ("followup", "noticed")
# A yearly date's or a deadline's subject (pulse_dates keys): it comes up at
# each of its lead times on purpose, each once by its ref, so a card open
# from the last lead time doesn't hold the next one back.
DATE_SUBJECTS = ("person:", "fact:", "deadline:")


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
        mine = (card is not None and card["kind"] in CARD_KINDS and pulse_store.card_open(target)
                and (not subject or not card.get("subject") or card["subject"] == subject))
        if kind == CLOSE:
            return (CLOSE, target, "") if mine else (DROP, None, "close_unknown")
        if mine and target not in closing:
            return UPDATE, target, ""
        kind = NEW
        why = ("update_unknown" if card is None or card["kind"] not in CARD_KINDS
               else "update_closed" if target in closing or not pulse_store.card_open(target)
               else "update_subject")
    else:
        why = ""
    if subject and not subject.startswith(DATE_SUBJECTS):
        open_now = [card for card in pulse_store.open_cards_with([subject], now - HOLD_S)
                    if int(card["id"]) not in closing]
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


def drawn_types(home: Any) -> tuple[str, ...]:
    """The chat card types a "drawn" pick may use: none until every Conduit
    that draws cards draws Pulse looks, then those whose feature every one
    of them has (as conduit_show leaves out what one can't draw)."""
    try:
        from .present_store import PresentStore
        present = PresentStore(home)
        if not present.has_feature_everywhere(LOOKS_FEATURE):
            return ()
        hidden = {kind for feature, kinds in card_schema.FEATURE_TYPES.items()
                  if not present.has_feature_everywhere(feature) for kind in kinds}
    except Exception:  # noqa: BLE001 - nothing drawn when it can't tell
        return ()
    return tuple(kind for kind in DRAWN_TYPES if kind not in hidden)


def rules(drawn: Sequence[str]) -> str:
    """PICK_RULES for a prompt, with the "drawn" shape when there are card
    types to draw it with."""
    text = PICK_RULES.replace("{{", "{").replace("}}", "}")
    if drawn:
        text += ("\n- \"drawn\" {\"body\": a card} when none of those shapes fits and a chart, weather, steps, stats, "
                 "a table or another card shows it better than words: the body is one conduit_show card of type "
                 + ", ".join(drawn) + ", with no buttons.")
    return text
