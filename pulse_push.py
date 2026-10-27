"""How a Pulse push goes: how much it may interrupt, and whether its words
stay off the Lock Screen (designs/pulse-where-you-are.md, "Push levels" and
"Surprises show no text on the Lock Screen"). Code decides, never the model.

The level (``level``), which the relay sets as the alert's
`interruption-level` (relay 0.20+, capability `push-levels`; an older relay
sends every push at the default level):

- time-sensitive: a pick held for a place at its moment, a reminder made
  due as the user got to (or left) its place, and a follow-up that can't
  wait (its check said it's urgent, or it went at night or in a closed part
  of the day because it would be no use by the time it may go). At most
  TIME_SENSITIVE_DAILY a day in the user's time zone; past that, active.
  iOS delivers it as active for an app without the Time Sensitive
  entitlement.
- passive (Notification Center, no lit screen or sound): anything else that
  goes while the phone says the user is busy (pulse_hours.busy: an event, or
  a Focus that holds calls; driving doesn't count), and a report whose
  pushed picks are all interests or far off (pulse_prefs' LATER and UNDATED
  bands).
- active: the rest, the morning brief included.

A push is private (``private_words``, ``private_item``, ``private_row``)
when it's about someone's birthday or anniversary, or its words are about a
gift, a surprise, health or money (English words, PRIVATE_WORDS). With
Hide private reminders on (PRIVATE_SETTING, on by default), its body is
PRIVATE_BODY whatever Show previews says, so its words never leave the host
in a push, and its sealed payload says `private` for Conduit to write it in
the app's language. Its card shows everything in the app.
"""

from __future__ import annotations

import re
from datetime import datetime, tzinfo
from typing import Any, Optional, Sequence

from . import pulse_candidates as candidates
from . import pulse_dates as dates
from . import pulse_hours as hours
from . import pulse_prefs as prefs

PASSIVE, ACTIVE, TIME_SENSITIVE = "passive", "active", "time-sensitive"
LEVELS = (PASSIVE, ACTIVE, TIME_SENSITIVE)
TIME_SENSITIVE_DAILY = 3
# Today's time-sensitive pushes: {"day": "YYYY-MM-DD" in the user's zone, "count"}.
TIME_SENSITIVE_KEY = "time_sensitive_today"
# A report whose pushed picks are all of these kinds, or in these bands, goes passive.
QUIET_KINDS = ("interest",)
QUIET_BANDS = (prefs.LATER, prefs.UNDATED)
# Hide private reminders on the Lock Screen (pulse_store.DEFAULTS): on unless set to False.
PRIVATE_SETTING = "private_pushes"
PRIVATE_BODY = "Pulse has something for you."

# Words that make a push private, whole words in any case. "Present" counts
# only as something given ("her present", "Sam's present"), and money's
# "bills" only in lower case (PRIVATE_BILLS): the Bills are a team.
PRIVATE_WORDS = re.compile(
    r"\b(?:"
    # A gift or a surprise
    r"gifts?|gifted|presents|(?:a|her|his|their|my|your|our|\w+['’]s) presents?|surprises?|surprised|"
    # Someone's day
    r"birthdays?|b-?days?|anniversar(?:y|ies)|turns \d{1,3}|"
    # Health
    r"doctors?|dentists?|therap(?:y|ies|ists?)|counsell?(?:ors?|ing)|psychiatrists?|psychologists?|"
    r"prescriptions?|pharmac(?:y|ies)|medications?|meds|medicines?|pills|diagnos(?:is|es|ed)|biops(?:y|ies)|"
    r"surgery|surgeries|hospitals?|clinics?|blood tests?|lab results|pregnan(?:t|cy)|fertility|ivf|"
    r"ultrasounds?|cancer|chemo(?:therapy)?|rehab|physio(?:therapy)?|"
    # Money
    r"banks?|banking|salary|salaries|paychecks?|payslips?|payday|loans?|debts?|mortgages?|overdrafts?|"
    r"credit cards?|credit scores?|tax(?:es)?|owes?|owed|money|savings|pensions?"
    r")\b",
    re.IGNORECASE,
)
PRIVATE_BILLS = re.compile(r"\bbills\b")
# A fact's yearly date, as pulse_dates.ref names its lead times.
_FACT_REF = re.compile(r"date:fact:([0-9]+):")


def hides_private(pulse_store: Any) -> bool:
    """Whether a private push says only PRIVATE_BODY (`pulse private on|off`,
    PUT /pulse's "private_pushes")."""
    return pulse_store.get(PRIVATE_SETTING) is not False


def private_words(text: Any) -> bool:
    """Whether ``text`` is about a gift, a surprise, someone's day, health or money."""
    return isinstance(text, str) and bool(PRIVATE_WORDS.search(text) or PRIVATE_BILLS.search(text))


def private_item(item: Optional[dict]) -> bool:
    """A remembered item that's a birthday or an anniversary every year, or
    whose words are private."""
    if item is None:
        return False
    return (bool(item.get("every_year")) and item.get("occasion") in dates.GIFTS) or private_words(item.get("text"))


def private_row(pulse_store: Optional[Any], row: dict) -> bool:
    """A report's or the look ahead's pick (a candidate row): its words, the
    remembered item it is or rests on, the yearly date it's a lead time of
    (pulse_dates.ref: a picked person's birthday, a fact's birthday or
    anniversary), or the remembered item a next step follows. A next step
    after an event goes by its why too, which names the event ("the dentist
    appointment ended this morning..."): the event's words aren't kept with
    it. With no ``pulse_store`` (it can't be read), a pick on a remembered
    item or a date, or a next step after one, is private."""
    if private_words(row.get("text")):
        return True
    ref = str(row.get("ref") or "")
    if ref.startswith(candidates.AFTER_EVENT):
        return private_words(row.get("why"))
    if ref.startswith("date:person:"):
        return True  # a picked person's birthday
    item_id = row.get("item_id") if row.get("origin") in (candidates.MEMORY, candidates.DATE) else None
    fact = _FACT_REF.match(ref)
    if fact:
        item_id = int(fact.group(1))
    after = ref[len(candidates.AFTER_ITEM):] if ref.startswith(candidates.AFTER_ITEM) else ""
    if after.isdecimal():
        item_id = int(after)
    if not isinstance(item_id, int) or isinstance(item_id, bool):
        return False
    return pulse_store is None or private_item(pulse_store.item(item_id))


def quiet_picks(picks: Sequence[dict]) -> bool:
    """Whether a report's pushed picks are all interests or far off."""
    return bool(picks) and all(pick.get("kind") in QUIET_KINDS or pick.get("band") in QUIET_BANDS
                               for pick in picks)


def level(pulse_store: Any, *, moment: bool, cant_wait: bool, quiet: bool, busy: Optional[tuple[str, Any]],
          zone: tzinfo, now: float) -> str:
    """A Pulse push's level: time-sensitive for ``moment`` (a place's
    moment); else passive while ``busy`` (pulse_hours.busy, driving aside);
    else time-sensitive for ``cant_wait`` (a follow-up that can't wait);
    else passive for ``quiet`` (a report's quiet picks); else active. A
    time-sensitive one counts toward today's TIME_SENSITIVE_DAILY, and past
    it is active."""
    if moment:
        return _time_sensitive(pulse_store, zone, now)
    if busy is not None and busy[0] != hours.DRIVING:
        return PASSIVE
    if cant_wait:
        return _time_sensitive(pulse_store, zone, now)
    return PASSIVE if quiet else ACTIVE


def _time_sensitive(pulse_store: Any, zone: tzinfo, now: float) -> str:
    """TIME_SENSITIVE, counted, while today has room; else ACTIVE. A count
    that can't be read is a full day's."""
    day = datetime.fromtimestamp(now, zone).date().isoformat()
    for _ in range(5):  # another process counting one at the same time
        stored = pulse_store.get(TIME_SENSITIVE_KEY)
        count = sent_today(stored, day)
        if count >= TIME_SENSITIVE_DAILY:
            return ACTIVE
        if pulse_store.swap(TIME_SENSITIVE_KEY, stored, {"day": day, "count": count + 1}):
            return TIME_SENSITIVE
    return ACTIVE


def sent_today(stored: Any, day: str) -> int:
    """How many time-sensitive pushes went on ``day`` (TIME_SENSITIVE_KEY's
    value): 0 for another day, TIME_SENSITIVE_DAILY for one that can't be read."""
    if not isinstance(stored, dict) or stored.get("day") != day:
        return 0
    count = stored.get("count")
    return count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else TIME_SENSITIVE_DAILY
