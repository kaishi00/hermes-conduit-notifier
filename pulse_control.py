"""Pulse as data, for Conduit's Settings > Pulse page and the CLI alike:
its state, the settings a tester changes, and the backfill's steps. Nothing
here prints; a step Pulse won't take raises Refused with a code that each
caller words its own way."""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from . import pulse, pulse_candidates, pulse_delivery, pulse_followup, pulse_hermes, pulse_learning, pulse_memory
from . import pulse_hours, pulse_quiet
from . import pulse_host as host
from . import pulse_report

RECENT = 5
ITEMS = 200
MAX_DAYS = 365


class Refused(Exception):
    """A step Pulse won't take now. ``code`` says why:

    - ``reading``: a backfill is already reading (``from``).
    - ``paused``: an unfinished backfill carries on first (``from``).
    - ``already_read``: memory has read back ``days`` already.
    - ``memory_off``: memory is off.
    - ``pulse_off``: Pulse is off.
    - ``no_home_chat``: Pulse has no home chat (``problem``).
    - ``unreadable``: Hermes's chats couldn't be read (``error``).
    - ``not_running`` / ``nothing_to_cancel``: no backfill to stop or cancel.
    - ``changed``: the backfill changed meanwhile; try again.
    - ``invalid``: a setting's value isn't one Pulse takes (``field``).
    """

    def __init__(self, code: str, **data: Any) -> None:
        super().__init__(code)
        self.code = code
        self.data = data


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def days_value(value: Any) -> Optional[int]:
    """A backfill's days, 1 to 365, or None."""
    return value if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= MAX_DAYS else None


def stored_days(pulse_store: Any) -> int:
    return days_value(pulse_store.get("memory_backfill_days")) or pulse.store.DEFAULTS["memory_backfill_days"]


def daily_tokens(pulse_store: Any) -> int:
    cap = pulse_store.get("memory_daily_tokens")
    return int(cap) if _number(cap) and cap > 0 else pulse.store.DEFAULTS["memory_daily_tokens"]


def _backfills(pulse_store: Any) -> tuple[Any, Optional[dict], Optional[dict]]:
    """The stored value, the backfill if it has a window, and that one if it's reading."""
    stored = pulse_store.get("memory_backfill")
    backfill = stored if pulse_memory.has_window(stored) else None
    return stored, backfill, backfill if backfill and backfill.get("active") else None


def backfill_state(pulse_store: Any) -> dict[str, Any]:
    """{"state": none|reading|paused|done|cancelled, "from", "to", "days",
    "narrow", "unreadable"}; "narrow" when a finished one read this profile
    only and memory now reads every profile."""
    _, backfill, _ = _backfills(pulse_store)
    if backfill is None:
        return {"state": "none"}
    state = ("cancelled" if backfill.get("cancelled") else "done" if backfill.get("done")
             else "reading" if backfill.get("active") else "paused")
    stalled = pulse_store.get("memory_backfill_unreadable")
    return {"state": state, "from": backfill["from"], "to": backfill["to"],
            "days": days_value(backfill.get("days")),
            "narrow": bool(backfill.get("done")) and not pulse_memory.covers_scope(pulse_store, backfill),
            "unreadable": [str(name) for name in stalled] if state == "reading" and isinstance(stalled, list) else []}


def backfill_stop(pulse_store: Any) -> None:
    _, _, active = _backfills(pulse_store)
    if not active:
        raise Refused("not_running")
    if not pulse_store.swap("memory_backfill", active, {**active, "active": False}):
        raise Refused("changed")


def backfill_cancel(pulse_store: Any) -> None:
    """Drops an unfinished backfill. What it read stays; memory counts as
    read back only to where it began."""
    _, backfill, _ = _backfills(pulse_store)
    if not backfill or backfill.get("done"):
        raise Refused("nothing_to_cancel")
    if not pulse_store.swap("memory_backfill", backfill, {"from": backfill["to"], "to": backfill["to"],
                                                         "active": False, "done": True, "cancelled": True,
                                                         "scope": pulse_memory.scope(pulse_store, backfill)}):
        raise Refused("changed")
    pulse_store.clear_backfill_marks()
    pulse_store.set("memory_backfill_unreadable", [])


def _window(pulse_store: Any, home: Path, days: int, now: float, *, starting: bool) -> tuple[tuple[float, float], Any]:
    """The window ``days`` reads and the home chat, or Refused."""
    _, backfill, active = _backfills(pulse_store)
    if active:
        raise Refused("reading", **{"from": active["from"]})
    if not starting and backfill and not backfill.get("done"):
        raise Refused("paused", **{"from": backfill["from"]})
    if starting and not pulse_store.get("memory"):
        raise Refused("memory_off")
    since = pulse_store.get("memory_since")
    if starting and not _number(since):
        # Memory hasn't run yet: set where live reading starts first (unless its first run
        # just did), so the window ends exactly there.
        pulse_store.swap("memory_since", since, now)
    window = pulse_memory.backfill_window(pulse_store, days, now)
    if window is None:
        raise Refused("already_read", days=days)
    chat, problem = pulse.home_chat(pulse_store, Path(home))
    if chat is None:
        raise Refused("no_home_chat", problem=problem)
    return window, chat


def backfill_estimate(pulse_store: Any, home: Path, days: Optional[int] = None,
                      now: Optional[float] = None) -> dict[str, Any]:
    """What reading back ``days`` (else the last asked) would cost:
    {"days", "from", "to", "chats", "chars", "chunks", "tokens",
    "daily_tokens", "days_needed", "unreadable"}. Remembers the days, so a
    start without them reads what was shown."""
    now = time.time() if now is None else now
    days = days_value(days) or stored_days(pulse_store)
    window, chat = _window(pulse_store, home, days, now, starting=False)
    try:
        cost = pulse_memory.estimate(pulse.memory_sources(pulse_store, Path(home), chat.chat_id), window)
    except pulse_hermes.HermesUnreadable as error:
        raise Refused("unreadable", error=type(error).__name__)
    pulse_store.set("memory_backfill_days", days)
    cap = daily_tokens(pulse_store)
    return {"days": days, "from": window[0], "to": window[1], "chats": cost["chats"], "chars": cost["chars"],
            "chunks": cost["chunks"], "tokens": cost["tokens"], "daily_tokens": cap,
            "days_needed": -(-cost["tokens"] // cap), "unreadable": list(cost["unreadable"])}


def backfill_start(pulse_store: Any, home: Path, days: Optional[int] = None,
                   now: Optional[float] = None) -> dict[str, Any]:
    """Starts (or carries on) reading back ``days`` (else the last asked):
    {"from", "to", "days", "resumed"}."""
    now = time.time() if now is None else now
    asked = days_value(days)
    days = asked or stored_days(pulse_store)
    stored, backfill, _ = _backfills(pulse_store)
    window, _ = _window(pulse_store, home, days, now, starting=True)
    resuming = isinstance(backfill, dict) and not backfill.get("done") \
        and (backfill.get("from"), backfill.get("to")) == window
    if resuming:
        days = days_value(backfill.get("days")) or days
    if not pulse_store.swap("memory_backfill", stored, {"from": window[0], "to": window[1], "days": days,
                                                       "active": True, "done": False, "started_at": now,
                                                       "scope": pulse_memory.scope(pulse_store,
                                                                                   backfill if resuming else None)}):
        raise Refused("changed")
    if not resuming:  # older windows' marks count as none; drop them
        pulse_store.clear_backfill_marks(keep_window=window[0])
    if asked:  # a later estimate or start without days reads the same
        pulse_store.set("memory_backfill_days", asked)
    pulse_store.set("memory_requested", True)
    return {"from": window[0], "to": window[1], "days": days, "resumed": resuming}


SETTINGS = ("enabled", "memory", "followups", "daily_report", "report_profiles", "report_times", "time_zone",
            "brief_time", "model", "nudge", "memory_daily_tokens", "followup_daily_tokens", "note",
            "quiet_days", "quiet_until", "night")
# What a daily token budget may be set to.
MIN_DAILY_TOKENS = 10_000
MAX_DAILY_TOKENS = 5_000_000
# How long the user's note to Pulse may be: a few paragraphs, and a bounded
# share of every prompt it goes into.
NOTE_LIMIT = 1_500


def _encodes(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def update(pulse_store: Any, body: Any, home: Any = None) -> None:
    """Applies the settings in ``body`` (any of SETTINGS). Every value is
    checked before any is written, so a bad one changes nothing; any other
    key is refused too. Values: booleans for "enabled", "memory", "followups" and "daily_report"; "all" or "this"
    for "report_profiles"; a list of "HH:MM" for "report_times"; an IANA
    name, or "" for the phone's, for "time_zone"; {"provider", "model"} for
    "model" (the model Pulse's turns run on), or null for the chat's own;
    "gentle", "balanced" or "eager" for "nudge"; a whole number of tokens,
    MIN_DAILY_TOKENS to MAX_DAILY_TOKENS, for "memory_daily_tokens" and
    "followup_daily_tokens" (null for follow-ups: the nudge level's); a
    string of at most NOTE_LIMIT characters for "note", what the user asked
    of Pulse in their own words ("" clears it); a list of weekdays (Monday
    0 to Sunday 6) for "quiet_days", when Pulse keeps to itself; null,
    "today", a day (YYYY-MM-DD) or epoch seconds for "quiet_until", when a
    quiet spell ends, in the user's time zone (``home`` finds it); two
    "HH:MM" (from, to) for "night", when nothing pushes (pulse_hours), or
    [] for none."""
    if not isinstance(body, dict):
        raise Refused("invalid", field="")
    for field in body:
        if field not in SETTINGS:
            raise Refused("invalid", field=str(field)[:64])
    changes: dict[str, Any] = {}
    for field in ("enabled", "memory", "followups", "daily_report"):
        if field in body:
            if not isinstance(body[field], bool):
                raise Refused("invalid", field=field)
            changes[field] = body[field]
    if "report_profiles" in body:
        if body["report_profiles"] not in ("all", "this"):
            raise Refused("invalid", field="report_profiles")
        changes["report_profiles"] = body["report_profiles"]
    if "report_times" in body:
        times = body["report_times"]
        parsed = pulse.report.parse_times(",".join(times)) if isinstance(times, list) and 0 < len(times) <= 12 \
            and all(isinstance(value, str) and len(value) <= 5 for value in times) else None
        if parsed is None:
            raise Refused("invalid", field="report_times")
        changes["report_time"] = parsed
    if "brief_time" in body:
        value = body["brief_time"]
        parsed = pulse.report.parse_times(value) if isinstance(value, str) and len(value) <= 5 else None
        if parsed is None or "," in parsed:
            raise Refused("invalid", field="brief_time")
        changes["brief_time"] = parsed
    if "time_zone" in body:
        zone = body["time_zone"]
        if not isinstance(zone, str) or len(zone) > 64 or (zone and not pulse.report.known_zone(zone)):
            raise Refused("invalid", field="time_zone")
        changes["time_zone"] = zone
    if "model" in body:
        changes[pulse.MODEL] = _model(body["model"])
    if "nudge" in body:
        if not isinstance(body["nudge"], str) or body["nudge"] not in pulse_learning.NUDGES:
            raise Refused("invalid", field="nudge")
        changes["nudge"] = body["nudge"]
    for field in ("memory_daily_tokens", "followup_daily_tokens"):
        if field in body:
            value = body[field]
            if value is None and field == "followup_daily_tokens":
                changes[field] = None
            elif isinstance(value, int) and not isinstance(value, bool) \
                    and MIN_DAILY_TOKENS <= value <= MAX_DAILY_TOKENS:
                changes[field] = value
            else:
                raise Refused("invalid", field=field)
    if "note" in body:
        value = body["note"].strip() if isinstance(body["note"], str) else None
        # Prose only: no control characters but line breaks and tabs, and
        # nothing UTF-8 can't carry (an unpaired surrogate from JSON).
        if value is None or len(value) > NOTE_LIMIT or any(ord(ch) < 32 and ch not in "\n\t" for ch in value) \
                or not _encodes(value):
            raise Refused("invalid", field="note")
        changes["note"] = value
    if "quiet_days" in body:
        value = body["quiet_days"]
        if not isinstance(value, list) or len(value) > len(pulse_quiet.DAYS) or any(
                isinstance(day, bool) or not isinstance(day, int) or not 0 <= day < len(pulse_quiet.DAYS)
                for day in value):
            raise Refused("invalid", field="quiet_days")
        changes["quiet_days"] = sorted(set(value))
    if "quiet_until" in body:
        changes["quiet_until"] = _quiet_until(pulse_store, body["quiet_until"], home)
    if "night" in body:
        night = pulse_hours.parse_night(body["night"])
        if night is None:
            raise Refused("invalid", field="night")
        changes[pulse_hours.SETTING] = night
    for key, value in changes.items():
        pulse_store.set(key, value)


def _quiet_until(pulse_store: Any, value: Any, home: Any) -> Optional[float]:
    """When a quiet spell ends: None clears it; "today" is the end of
    today, or the spell already set when that ends later (Quiet for today
    never shortens a vacation); a day (YYYY-MM-DD) is its start; a number
    is epoch seconds. In the user's zone, ahead of now by at most
    pulse_quiet.MAX_AHEAD_S."""
    if value is None:
        return None
    now = time.time()
    zone = pulse.report_zone(pulse_store, Path(home) if home else Path("."), now)
    if value == "today":
        end = pulse_quiet.next_midnight(zone, now)
        current = pulse_quiet.until(pulse_store)
        return current if current is not None and current > end else end
    if isinstance(value, str):
        at = pulse_quiet.day_start(zone, value)
    else:
        at = pulse_quiet.seconds(value)
    if at is None or not now < at <= now + pulse_quiet.MAX_AHEAD_S:
        raise Refused("invalid", field="quiet_until")
    return at


def _model(value: Any) -> Optional[dict[str, str]]:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {"provider", "model"}:
        raise Refused("invalid", field="model")
    name, provider = value.get("model"), value.get("provider", "")
    if not isinstance(name, str) or not isinstance(provider, str) or not name.strip() \
            or len(name) > 200 or len(provider) > 100 or not host.plain_flag_value(name) \
            or (provider and not host.plain_flag_value(provider)):
        raise Refused("invalid", field="model")
    return {"provider": provider, "model": name}


def request_report(pulse_store: Any) -> None:
    if not pulse_store.get("enabled"):
        raise Refused("pulse_off")
    pulse_store.set("report_requested", True)


def forget(pulse_store: Any, item_id: Any) -> bool:
    if not isinstance(item_id, int) or isinstance(item_id, bool) or item_id < 1:
        raise Refused("invalid", field="id")
    return pulse_store.close_item(item_id, status="cancelled", closed_by="user")


def items(pulse_store: Any, kind: str = "") -> list[dict[str, Any]]:
    """Open memory items, most recently mentioned first."""
    if kind and kind not in pulse_memory.KINDS:
        raise Refused("invalid", field="kind")
    return [{"id": item["id"], "kind": item["kind"], "text": item["text"], "evidence": item["evidence"],
             "explicit": bool(item["explicit"]), "due": item["due"], "due_at": item["due_at"],
             "mentioned_at": item["mentioned_at"], "profile": item["profile"], "chat_id": item["chat_id"]}
            for item in pulse_store.open_items(kind, limit=ITEMS)]


def followups(pulse_store: Any, limit: int = ITEMS) -> list[dict[str, Any]]:
    """Promises Pulse checks on, most recently decided first: the journal."""
    out = []
    for row in pulse_store.followups(limit):
        item = pulse_store.item(row["item_id"])
        out.append({"id": row["item_id"], "text": item["text"] if item else "", "evidence": item["evidence"] if item else "",
                    "profile": item["profile"] if item else "", "chat_id": item["chat_id"] if item else "",
                    "message_id": item["message_id"] if item else None,
                    "decision": row["decision"] or "waiting", "why": row["why"], "news": row["news"],
                    "checks": row["checks"], "next_check_at": row["next_check_at"], "updated_at": row["updated_at"]})
    return out


def feed(pulse_store: Any, home: Optional[Path] = None, limit: int = pulse_learning.CARDS) -> dict[str, Any]:
    """The Pulse screen: what Pulse brought the user, newest first, how many
    are unseen, each kind's loudness, and the Pulse chat's id ("" when
    there's none), which Conduit keeps behind the screen."""
    # Pulse off: no chat stands behind the screen, whatever is titled Pulse.
    on = home is not None and pulse_store.get("enabled")
    chat = pulse.home_chat(pulse_store, Path(home))[0] if on else None
    cards = pulse_store.cards(limit)
    marks = pulse_store.done_marks([card["id"] for card in cards])
    now = time.time()
    zone = pulse.report_zone(pulse_store, Path(home) if home is not None else Path("."), now)
    return {"cards": [pulse_learning.card_entry(card, marks.get(card["id"])) for card in cards],
            "unseen": pulse_store.unseen_cards(), "levels": pulse_learning.levels(pulse_store),
            "home_chat_id": chat.chat_id if chat else "",
            # The screen says when Pulse is quiet (pulse_quiet) and until when.
            "quiet_now": pulse_quiet.why(pulse_store, zone, now),
            "quiet_ends_at": pulse_quiet.ends_at(pulse_store, zone, now),
            # The night (pulse_hours): Not now's "later today" ends when it starts,
            # and at night it's just before the morning brief.
            "night": pulse_hours.night_text(pulse_store),
            "night_now": pulse_hours.is_night(pulse_store, zone, now),
            "brief_time": pulse.report.clock_text(pulse_store.get("brief_time") or pulse.brief.DEFAULT_TIME)}


def see(pulse_store: Any, ids: Any, now: Optional[float] = None) -> int:
    """Marks cards seen: a list of ids, or every card for None."""
    if ids is not None and not (isinstance(ids, list) and len(ids) <= 500 and all(
            isinstance(value, int) and not isinstance(value, bool) for value in ids)):
        raise Refused("invalid", field="ids")
    return pulse_store.see_cards(ids, time.time() if now is None else now)


def _learning(action: Any) -> Any:
    try:
        return action()
    except pulse_learning.Invalid as error:
        raise Refused("invalid", field=error.field) from None


def rate(pulse_store: Any, card_id: Any, rating: Any, reason: Any = "", now: Optional[float] = None,
         home: Any = None) -> list[dict]:
    """Rates a card; the lessons it made, as `learned` lists them. Refused
    while Pulse is off, as setting a level is: a rating can move one.
    "Wrong time" swipes the card away until a better time
    (pulse_followup.wrong_time_rest) in the user's time zone (``home``
    gives it: pulse.report_zone)."""
    if not pulse_store.get("enabled"):
        raise Refused("pulse_off")
    now = time.time() if now is None else now
    zone = pulse.report_zone(pulse_store, Path(home) if home else Path("."), now)
    rest_until = pulse_followup.wrong_time_rest(pulse_store, zone, now) if reason == pulse_learning.WRONG_TIME \
        else None
    made = _learning(lambda: pulse_learning.rate(pulse_store, card_id, rating, reason, now=now, zone=zone,
                                                 rest_until=rest_until))
    return [pulse_learning.lesson_entry(lesson) for lesson in made if lesson]


def done(pulse_store: Any, card_id: Any, is_done: Any = True, now: Optional[float] = None) -> bool:
    """Marks a follow-up or report item card done (a promise behind it
    closes, no rating), or Undo with False; whether it changed."""
    return _learning(lambda: pulse_learning.done(pulse_store, card_id, is_done,
                                                 now=time.time() if now is None else now))


def dismiss(pulse_store: Any, card_id: Any, dismissed: Any = True, now: Optional[float] = None,
            until: Any = None, home: Any = None) -> bool:
    """Swipes a card away ("Not now", nothing learned) until later today,
    tomorrow or next week (``until``, pulse_followup.RESTS: it comes back
    as new then) or for good, or puts it back with False; whether it
    changed. ``home`` gives the user's time zone (pulse.report_zone)."""
    now = time.time() if now is None else now
    rest_until = None
    if until is not None and dismissed is True:
        if not isinstance(until, str) or until not in pulse_followup.RESTS:
            raise Refused("invalid", field="until")
        if not isinstance(card_id, int) or isinstance(card_id, bool) or pulse_store.card(card_id) is None:
            raise Refused("invalid", field="card")  # before the zone is read for a card that isn't there
        zone = pulse.report_zone(pulse_store, Path(home) if home else Path("."), now)
        rest_until = pulse_followup.rest_time(until, now, zone, pulse_store)
    return _learning(lambda: pulse_learning.dismiss(pulse_store, card_id, dismissed, now=now, rest_until=rest_until))


def acted(pulse_store: Any, card_id: Any, now: Optional[float] = None) -> bool:
    """Do it on a follow-up or report item card: Pulse reads the chat Do it
    opened once it has answered and marks the card done if it got done;
    whether a check was booked."""
    return _learning(lambda: pulse_followup.act(pulse_store, card_id, now=time.time() if now is None else now))


def learned(pulse_store: Any, home: Optional[Path] = None, now: Optional[float] = None) -> dict[str, Any]:
    """What ratings taught Pulse, newest first, each kind's loudness, and
    what learning holds about each thing it has votes for, strongest first
    (pulse_prefs' Learned.preferences), a chat's with its profile, id and
    title, as What Pulse left out names it (``_chat_title``). A reset
    lesson about a chat carries the same."""
    found = pulse_store.learned(time.time() if now is None else now).preferences()
    for entry in found:
        if entry["scope"] == "place":
            entry.update(_chat_of(pulse_store, home, entry["value"]))
    lessons = [_reset_entry(pulse_store, home, lesson) for lesson in pulse_learning.learned(pulse_store)]
    return {"lessons": lessons, "levels": pulse_learning.levels(pulse_store), "preferences": found}


def _reset_entry(pulse_store: Any, home: Optional[Path], lesson: dict) -> dict:
    """A lesson as `learned` lists it: a reset about a chat with the chat's
    profile, id and title, and its text naming the chat."""
    if lesson["what"] == pulse_learning.RESET and lesson["kind"] == "place" and lesson["value"].startswith("chat:"):
        lesson.update(_chat_of(pulse_store, home, lesson["value"]))
        lesson["text"] = pulse_learning.describe(lesson)
    return lesson


def _chat_of(pulse_store: Any, home: Optional[Path], place: str) -> dict[str, str]:
    """A chat place's profile, id and title (pulse_prefs.place); {} for
    another place, or one that names no chat."""
    parts = place.split(":", 2)
    if parts[0] != "chat" or len(parts) != 3:
        return {}
    _, profile, chat_id = parts
    return {"profile": profile, "chat_id": chat_id,
            "chat_title": _chat_title(pulse_store, home, profile, chat_id) if chat_id else ""}


def reset(pulse_store: Any, scope: Any, value: Any = "", now: Optional[float] = None,
          home: Optional[Path] = None) -> dict:
    """Forgets what learning holds about one thing, or everything
    (pulse_learning.reset); the lesson, which Undo takes back. Refused
    while Pulse is off, as a rating is."""
    if not pulse_store.get("enabled"):
        raise Refused("pulse_off")
    lesson = _learning(lambda: pulse_learning.reset(pulse_store, scope, value, time.time() if now is None else now))
    return _reset_entry(pulse_store, home, pulse_learning.lesson_entry(lesson))


# What Pulse left out: the journal's candidates from the last LEFT_OUT_S
# (or its latest report's, when that's older) that didn't come up, at most
# LEFT_OUT of them, those learning or the user's own rules kept out first.
LEFT_OUT_S = 86400.0
LEFT_OUT = 100
# The journal rows read for it, newest first: far more than a day's lists
# hold, so the order above is kept whatever ``limit`` cuts.
LEFT_OUT_ROWS = 5000
LEFT_OUT_ORDER = (pulse_candidates.QUIETED, pulse_candidates.MUTED, pulse_candidates.OFF,
                  pulse_candidates.OVER_LIMIT, pulse_candidates.SKIPPED, pulse_candidates.NOT_PICKED,
                  pulse_candidates.RESTING, pulse_candidates.UNANSWERED)


def _on_the_way(pulse_store: Any) -> str:
    """The report run whose picks are on their way to the Pulse screen, its
    list made and its report ready or sent, not refused: what it picked has
    come up already, though its cards haven't landed. What a run that was
    dropped or refused picked never came up. "" for none."""
    run = pulse_store.get(pulse.REPORT_RUN)
    run = run if isinstance(run, dict) else {}
    going = run.get("state") in (pulse.RUN_READY, pulse.RUN_SENT) and not run.get("refused")
    return str(run.get("id") or "") if going else ""


def _left_out_since(pulse_store: Any, now: float) -> float:
    """Where What Pulse left out starts: the last day, or the latest report
    when that's older."""
    latest = pulse_store.latest_candidate_at()
    return min(now - LEFT_OUT_S, latest) if latest is not None else now - LEFT_OUT_S


def left_out(pulse_store: Any, home: Optional[Path] = None, now: Optional[float] = None,
             limit: int = LEFT_OUT) -> list[dict[str, Any]]:
    """What the latest reports considered and left out, and why, for What
    Pulse left out: each thing once (a remembered item, or the same text, by
    its newest decision), not one that came up in that report or later,
    learning's and the user's rules' first, then the rest, newest and most
    valued first within each. One brought up (``bring_up``) says which card
    it became, whichever report's candidate was brought up, however long
    ago."""
    seen: set[Any] = set()
    out = []
    for row in pulse_store.left_out(_left_out_since(pulse_store, time.time() if now is None else now), LEFT_OUT_ROWS,
                                    _on_the_way(pulse_store)):
        # One report can know a thing as a remembered item and another as
        # words alone, and an item's words can change: it is one thing.
        if row["thing"] in seen or row["later"] or not row["text"]:
            continue
        seen.add(row["thing"])
        out.append({"id": row["id"], "created_at": row["created_at"], "kind": row["kind"], "text": row["text"],
                    "why": row["why"], "decision": row["decision"], "skip": row["skip"], "value": row["value"],
                    "learned": pulse_learning.factor_entry(row["factor"]), "origin": row["origin"],
                    "source": row["source"], "area": row["area"], "profile": row["profile"] or "",
                    "chat_id": row["chat_id"] or "", "chat_title": "", "item_id": row["item_id"],
                    "due_at": row["due_at"], "card_id": row["card_id"]})
    order = {decision: n for n, decision in enumerate(LEFT_OUT_ORDER)}
    out.sort(key=lambda entry: (order.get(entry["decision"], len(order)), -entry["created_at"],
                                -(entry["value"] or 0), entry["id"]))
    out = out[:limit]
    # Titles only for what's listed: each can be a read of Hermes's chats.
    titles: dict[tuple[str, str], str] = {}
    for entry in out:
        chat = (entry["profile"], entry["chat_id"])
        if chat[1] and chat not in titles:
            titles[chat] = _chat_title(pulse_store, home, *chat)
        entry["chat_title"] = titles.get(chat, "")
    return out


def _chat_title(pulse_store: Any, home: Optional[Path], profile: str, chat_id: str) -> str:
    """A chat's title as What Pulse learned names it, the one its newest
    card gave it, or as Hermes has it for a chat no card came from yet."""
    return pulse_store.card_chat_title(profile, chat_id) or (
        pulse_followup.chat_title(Path(home), {"profile": profile, "chat_id": chat_id}) if home is not None else "")


def bring_up(pulse_store: Any, candidate_id: Any, home: Optional[Path] = None,
             now: Optional[float] = None) -> dict[str, Any]:
    """Makes a card on the Pulse screen of something a report left out
    (``left_out``), as a report card: its thumbs, Do it and Done teach Pulse
    about things like it. The card it already became, if it or another
    candidate about the same thing did. Refused while Pulse is off, and for
    a candidate whose thing came up or the journal no longer has."""
    if not pulse_store.get("enabled"):
        raise Refused("pulse_off")
    if not isinstance(candidate_id, int) or isinstance(candidate_id, bool):
        raise Refused("invalid", field="id")
    found = pulse_store.candidate(candidate_id)
    if found is None or found["decision"] == pulse_candidates.SURFACED or not found["text"]:
        raise Refused("invalid", field="id")
    now = time.time() if now is None else now
    row = pulse_learning.left_out_card(
        pulse_store, found, kinds=pulse_report.KINDS, now=now,
        item_chat_title=lambda item: _chat_title(pulse_store, home, item["profile"], item["chat_id"]))
    card_id = pulse_store.bring_up(candidate_id, row, _left_out_since(pulse_store, now), _on_the_way(pulse_store))
    card = pulse_store.card(card_id) if card_id is not None else None
    if card is None:
        raise Refused("invalid", field="id")
    return pulse_learning.card_entry(card, pulse_store.done_marks([card["id"]]).get(card["id"]))


def undo(pulse_store: Any, lesson_id: Any, now: Optional[float] = None) -> bool:
    return _learning(lambda: pulse_learning.undo(pulse_store, lesson_id, time.time() if now is None else now))


def set_level(pulse_store: Any, kind: Any, level: Any, now: Optional[float] = None) -> Optional[dict]:
    if not pulse_store.get("enabled"):
        raise Refused("pulse_off")
    lesson = _learning(lambda: pulse_learning.set_level(pulse_store, kind, level,
                                                        time.time() if now is None else now))
    return pulse_learning.lesson_entry(lesson) if lesson else None


def state(pulse_store: Any, home: Path, now: Optional[float] = None) -> dict[str, Any]:
    """Everything Settings > Pulse shows, as data. Times are epoch seconds."""
    now = time.time() if now is None else now
    chat, problem = pulse.home_chat(pulse_store, Path(home))
    ticked = pulse_store.get("last_tick_at")
    running = _number(ticked) and now - ticked <= pulse.STALE_TICK_S
    lease = pulse_store.get(pulse_delivery.store.LEADER)
    zone = pulse.report_zone(pulse_store, Path(home), now)
    usage = pulse_store.get("memory_usage")
    usage = usage if isinstance(usage, dict) else {}
    day = datetime.fromtimestamp(now, zone).strftime("%Y-%m-%d")
    used, _ = pulse._spent_today(pulse_store, "memory", day)
    follow_used, follow_cap = pulse._spent_today(pulse_store, "followup", day)
    profiles = pulse_store.get("report_profiles")
    follow_usage = pulse_store.get("followup_usage")
    follow_usage = follow_usage if isinstance(follow_usage, dict) else {}
    return {
        "enabled": bool(pulse_store.get("enabled")),
        "running": bool(running),
        "stale_build": bool(running and pulse.lease_on_older_build(lease)),
        "home_chat": {"id": chat.chat_id, "title": chat.title, "pinned": bool(chat.pinned)} if chat else None,
        "home_problem": problem,
        "home_title": pulse_store.get("home_title") or "Pulse",
        "model": pulse.turn_model(pulse_store),
        "checks": pulse_store.counts(pulse.PROBE),
        "recent": [{"at": row["created_at"], "kind": row["kind"], "state": row["state"],
                    "detail": row.get("detail") or ""} for row in pulse_store.recent(RECENT)],
        "daily_report": bool(pulse_store.get("daily_report")),
        "report_times": [f"{hour:02d}:{minute:02d}"
                         for hour, minute in pulse.report.report_times(pulse_store.get("report_time"))],
        "time_zone": str(zone),
        "time_zone_set": pulse_store.get("time_zone") or "",
        "next_report_at": pulse.report.next_time(now, zone, pulse_store.get("report_time"))
        if pulse_store.get("daily_report") else None,
        "report_profiles": "all" if profiles == "all" else "this",
        "other_profiles": [name for name, _ in pulse.report.profile_homes(Path(home))] if profiles == "all" else [],
        "memory": bool(pulse_store.get("memory")),
        "memory_counts": pulse_store.item_counts(),
        "followups": bool(pulse_store.get("followups")),
        # Follow-ups run with memory, which finds the promises: off without it.
        "followups_active": bool(pulse_store.get("enabled") and pulse_store.get("followups")
                                 and pulse_store.get("memory")),
        "followup_counts": pulse_store.followup_counts(),
        "followup_usage": {key: int(follow_usage.get(key, 0) or 0) for key in ("checks", "tokens", "failed")},
        "followup_daily_tokens": follow_cap,
        "followup_tokens_today": follow_used,
        # Whether the budget was set, or comes with the nudge level.
        "followup_daily_tokens_set": _number(pulse_store.get("followup_daily_tokens")),
        "nudge": pulse_learning.nudge(pulse_store),
        "note": pulse.report.note(pulse_store),
        "levels": pulse_learning.levels(pulse_store),
        "unseen_cards": pulse_store.unseen_cards(),
        "brief_time": pulse.report.clock_text(pulse_store.get("brief_time") or pulse.brief.DEFAULT_TIME),
        # Quiet times (pulse_quiet): the weekdays, the spell set, and whether it's quiet now and until when.
        "quiet_days": pulse_quiet.days(pulse_store),
        "quiet_until": pulse_quiet.until(pulse_store),
        "quiet_now": pulse_quiet.why(pulse_store, zone, now),
        "quiet_ends_at": pulse_quiet.ends_at(pulse_store, zone, now),
        # The night (pulse_hours): ["22:00", "07:00"], or [] for none; and whether it's night now.
        "night": pulse_hours.night_text(pulse_store),
        "night_now": pulse_hours.is_night(pulse_store, zone, now),
        "held_cards": pulse_store.held_count(),
        "memory_chunks": int(usage.get("chunks", 0) or 0),
        "memory_tokens": int(usage.get("tokens", 0) or 0),
        "daily_tokens": daily_tokens(pulse_store),
        "tokens_today": used,
        "backfill": backfill_state(pulse_store),
        "backfill_days": stored_days(pulse_store),
        "ratings": {kind: {"yes": yes, "no": no} for kind, (yes, no) in pulse.rating_counts(pulse_store).items()},
    }
