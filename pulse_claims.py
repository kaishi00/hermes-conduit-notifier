"""Hermes's claims, kept to judge (designs/pulse-what-comes-next.md,
"Hermes's claims, kept to judge").

Memory (pulse_memory) also notes what Hermes said in a chat it had just
done with a tool: set a reminder, an alarm or a timer, added or changed a
calendar event, created or changed a scheduled job, or sent a message. Each
keeps its quote, checked like any evidence, and code looks in the outcome
ledger (pulse_ledger) for the call behind it: one that worked, in the same
chat and the same turn (from the user's message before the claim to their
next one), of a tool that could do that (``does``). A claim with none is
what a card would one day bring up: "Hermes said it set a reminder for 5pm,
but none was made."

Nothing is shown yet: `hermes conduit-push pulse claims` lists them, to
judge first, as moments with nothing waiting were in step 5. Each is kept
for KEEP_S with what was said, when and in which chat, the call that backed
it, if one did, and the calls its turn made.

Only this profile's chats, as memory reads them once they go quiet (not a
backfill of older chats), and only turns from when the ledger has been
recording (FROM, which the loop sets once Pulse is on and turning Pulse off
clears): the ledger keeps this profile's calls alone, and none while Pulse
is off.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional, Sequence

from . import pulse_hermes as hermes
from . import pulse_ledger as ledger
from . import pulse_store as store

ACTIONS = ("reminder", "event", "job", "message")
MAX_CLAIMS = 6  # a chunk's
KEEP_S = 30 * 86400.0
# The turn's calls read to judge a claim, and kept with it to show.
TURN_CALLS = 500
SHOWN_CALLS = 12
# When the ledger started recording for good: claims are judged in turns
# from then on (``start``, ``stop``).
FROM = "claims_from"

# A call that does each, by its action: Conduit's calendar and phone tools.
CHANGES = {
    "reminder": ("add_reminder", "update_reminder", "set_alarm", "set_timer"),
    "event": ("add_event", "update_event"),
    "job": (),
    "message": (),
}
# Hermes's cron tool (cronjob_manage; cronjob before it) and Conduit's
# scheduled calls (a call at a time reminds as well).
CRON_TOOLS = ("cronjob", "cronjob_manage")
CRON_CHANGES = ("create", "update", "resume")
CALL_WATCH = "conduit_call_watch"
CALL_WATCH_CHANGES = ("set", "sync")
# A call that runs other tools or code might have done any of them: its own
# calls may not be this chat's in the ledger.
RUNS_OTHERS = ("delegate_task", "execute_code", "terminal", "process", "process_manage")
# Any other tool (a connector's, another plugin's, an older Hermes's) by a
# word of its name or action that starts with one of these, unless one
# says it only read.
NAMED = {
    "reminder": ("remind", "alarm", "timer", "todo", "task", "cron", "schedule"),
    "event": ("event", "calendar", "meeting"),
    "job": ("cron", "schedule", "job"),
    "message": ("send", "message", "mail", "email", "sms", "post", "reply", "notify"),
}
READS = ("list", "get", "search", "read", "find", "fetch", "query", "view", "lookup", "status")
REMOVES = ("delete", "remove", "cancel", "clear", "unset")
_WORD = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")


def start(pulse_store: store.PulseStore, now: float) -> None:
    """The loop, with Pulse on: claims are judged from ``now`` unless they
    already are."""
    since = pulse_store.get(FROM)
    if not isinstance(since, (int, float)) or isinstance(since, bool):
        pulse_store.set(FROM, now)


def stop(pulse_store: store.PulseStore) -> None:
    """Pulse turned off: the ledger stops, so judging starts over once the
    loop runs again."""
    pulse_store.set(FROM, None)


def judged_from(pulse_store: store.PulseStore) -> Optional[float]:
    """The first moment a turn can start and be judged: FROM, plus the time
    every process takes to see Pulse on (pulse_ledger.ENABLED_TTL_S); None
    while there's none."""
    since = pulse_store.get(FROM)
    if not isinstance(since, (int, float)) or isinstance(since, bool):
        return None
    return float(since) + ledger.ENABLED_TTL_S


def does(outcome: dict, action: str) -> bool:
    """Whether a ledger row's call could have done ``action`` (ACTIONS)."""
    tool = str(outcome.get("tool") or "").strip().lower()
    act = str(outcome.get("action") or "").strip().lower()
    if tool in RUNS_OTHERS:
        return True
    if tool in CRON_TOOLS:
        return action in ("reminder", "job") and act in CRON_CHANGES
    if tool == CALL_WATCH:
        return action == "reminder" and act in CALL_WATCH_CHANGES
    if act in CHANGES[action]:
        return True
    if tool.startswith("conduit_"):
        return False  # its actions say what it did
    # Words as written: sendMessage and send_message alike.
    words = [word.lower() for word in _WORD.findall(f"{outcome.get('tool') or ''} {outcome.get('action') or ''}")]
    return (any(word.startswith(NAMED[action]) for word in words)
            and not any(word.startswith(READS + REMOVES) for word in words))


def turn(rows: Sequence[Any], row_id: int, home: Path, chat_id: str) -> Optional[tuple[float, Optional[float]]]:
    """The turn row ``row_id`` of ``rows`` (a chat's rows, oldest first) is
    in: from the user's message before it, in ``rows`` or else earlier in
    the chat, to their next one (None while there's none). None when no
    message of the user's comes before it, or the chat can't be read."""
    index = next((n for n, row in enumerate(rows) if row["id"] == row_id), None)
    if index is None:
        return None
    start_at = next((row["timestamp"] for row in reversed(rows[:index]) if row["role"] == "user"), None)
    if start_at is None:
        try:
            before = hermes.user_row_before(home, chat_id=chat_id, before_id=row_id, max_chars=1)
        except hermes.HermesUnreadable:
            return None  # the claim goes unjudged, not the chunk unread
        if before is None:
            return None
        start_at = before["timestamp"]
    end_at = next((row["timestamp"] for row in rows[index + 1:] if row["role"] == "user"), None)
    return float(start_at), None if end_at is None else float(end_at)


def judge(pulse_store: store.PulseStore, home: Path, chat_id: str, found: Sequence[dict], rows: Sequence[Any], *,
          now: float) -> list[dict]:
    """The claims memory ``found`` in a chunk of ``rows`` (pulse_memory),
    each with its turn, the call that backed it ("" for none) and its
    turn's calls, as pulse_store.advance keeps them; the ones in a turn
    that started before claims are judged (``judged_from``) are left out."""
    since = judged_from(pulse_store)
    if since is None:
        return []
    judged = []
    for claim in found:
        bounds = turn(rows, claim["message_id"], home, chat_id)
        if bounds is None or bounds[0] < since:
            continue
        calls = sorted(pulse_store.outcomes_between(chat_id, bounds[0], now if bounds[1] is None else bounds[1],
                                                    TURN_CALLS), key=lambda row: (row["at"], row["id"]))
        backing = next((row for row in calls if row["status"] == "ok" and does(row, claim["action"])), None)
        lines = [ledger.line(row) for row in calls[:SHOWN_CALLS]]
        if len(calls) > SHOWN_CALLS:
            lines.append(f"(and {len(calls) - SHOWN_CALLS} more)")
        judged.append({**claim, "turn_from": bounds[0], "turn_to": bounds[1],
                       "backed_by": ledger.line(backing) if backing else "",
                       "calls_json": json.dumps(lines, ensure_ascii=False), "kept_at": now})
    return judged
