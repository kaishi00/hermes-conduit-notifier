"""What a Pulse turn may call (proactivity.md 7.6; designs/pulse-look-first.md).

Default deny: an allowlist kept in code, each entry with the reason it has no
side effects. Pulse's tool hook (pulse.pre_tool_call) asks ``check`` about
every call a Pulse turn makes. Nothing here rests on what the model says a
call is for: code decides from the tool, its arguments and the turn.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

logger = logging.getLogger(__name__)

# The Pulse turns that may look things up (pulse.REPORT, FOLLOWUP and BRIEF);
# a probe gets nothing.
LOOK_KINDS = frozenset({"report", "followup", "brief"})
FOLLOWUP = "followup"
# A follow-up about what the user or Hermes said in a chat may search the web.
# A watch's check reads the Shortcuts inbox (other people's words), and
# reports and briefs carry it, so they never do.
SEARCH_ITEM_KINDS = frozenset({"promise", "commitment"})


@dataclass(frozen=True)
class Entry:
    label: str  # what Why? and `pulse looks` call it
    actions: frozenset  # the actions allowed; empty for a tool that takes none
    reason: str  # why it has no side effects


ALLOWED: dict[str, Entry] = {
    "conduit_calendar": Entry("calendar", frozenset({"overview", "calendars", "events", "free_busy", "next",
                                                     "reminders"}),
                              "Reads the calendar and reminders the phone synced to this host: no network, no log. "
                              "Not pending: it marks the phone's answers reported."),
    "conduit_phone": Entry("phone", frozenset({"status", "alarms"}), "The same snapshot. Not pending."),
    "conduit_contacts": Entry("contacts", frozenset({"people"}), "The same snapshot."),
    "conduit_maps": Entry("maps", frozenset({"search", "lookup", "reverse_geocode", "directions", "travel_times"}),
                          "Asks Apple Maps from this host or the phone, within Maps' own budgets: the query leaves "
                          "the host, and each call goes in the location read log. \"me\" or a saved place only "
                          "with Where you are on (Turn.where). Navigate is a write."),
    "conduit_location": Entry("location", frozenset({"now", "places", "history", "distance_to"}),
                              "Reads where the user is from what Conduit shared with this host, only with Where you "
                              "are on (Turn.where): no network, and each read goes in the location read log. Not "
                              "locate, which asks the phone, nor the rules, which change things."),
    "web_search": Entry("web", frozenset(),
                        "Sends the query to Hermes's search provider. Only on a follow-up about a promise or "
                        "commitment, and never with a web address in it, so no search fetches a page with the "
                        "user's data in its address."),
}
LABELS = tuple(entry.label for entry in ALLOWED.values())
# Tool Search's bridge. Listing and describing tools changes nothing. Hermes
# unwraps tool_call before the hooks run, so they see the tool it calls by
# its own name; when it can't, the hook sees tool_call, and Hermes runs the
# calls inside it without asking again (room_rules._called_names), so each
# is checked here.
BRIDGE_CALL = "tool_call"
BRIDGE_READS = frozenset({"tool_search", "tool_describe"})
# The calendar's adds: never run on a Pulse turn. Called directly, each is
# kept as a suggestion the user adds with one tap (pulse_proposals).
ADDS = frozenset({"add_event", "add_reminder"})
MAX_PROPOSALS = 2  # pulse_proposals.MAX_PER_TURN
# Look-ups a turn may make, and a day's by label.
MAX_PER_TURN = 6
DAILY = {"maps": 20, "web": 10}
MAX_QUERY = 400
# A web address, or what fetching one needs: a scheme, www., a domain with a
# port, path or query, or an IP. With a scheme, a host written any other way
# (one number, hex) is caught by the scheme; without one, nothing reads it as
# an address.
_ADDRESS = re.compile(r"://|\bwww\.|\b[\w-]+\.[a-z]{2,}(?::\d{1,5}\b|[/?#])|\b\d{1,3}(?:\.\d{1,3}){3}\b",
                      re.IGNORECASE)

NO_TOOLS = "Pulse turns can't use tools."
USES = ("conduit_calendar (overview, calendars, events, free_busy, next, reminders), conduit_phone (status, "
        "alarms), conduit_contacts (people) and conduit_maps (search, lookup, reverse_geocode, directions, "
        "travel_times, between places you name: never \"me\" or a saved place)")
# With Where you are on.
WHERE_USES = ("conduit_calendar (overview, calendars, events, free_busy, next, reminders), conduit_phone (status, "
              "alarms), conduit_contacts (people), conduit_location (now, places, history, distance_to: where the "
              "user is) and conduit_maps (search, lookup, reverse_geocode, directions, travel_times)")
SEARCH_USE = ", and web_search (a few words: no web addresses, nothing private about the user)"
# Appended to a report's, follow-up's or brief's note while look_first is on.
LOOK_NOTE = ("Before you answer you may look things up, at most {limit} times, to check what the message brings "
             "up or add a detail to it: {uses}. Don't bring up anything new from what you find. If adding "
             "something to the calendar or reminders would help, conduit_calendar's add_event or add_reminder "
             "makes it a suggestion the user adds with one tap, at most {proposals} a turn: say you suggested it, "
             "never that it's done. Every other tool is blocked.")
NOT_ALLOWED = "Pulse turns can't use {what}. They may look things up with {uses}."
WHERE_BLOCK = ("Pulse turns can't use where the user is: name the places (\"near\" and \"from\" default to "
               "\"me\"), and not a saved place like home or work.")
LOCATION_BLOCK = ("Pulse turns can't read where the user is: that needs Where you are on in Pulse's settings, and "
                  "location shared in the background. Write the message without it.")
SEARCH_BLOCK = "Pulse can search the web only on a follow-up about something said in a chat."
ADDRESS_BLOCK = "A Pulse search can't have a web address in it: search with words."
QUERY_BLOCK = "web_search needs a query of up to 400 characters."
TURN_CAP_BLOCK = "That's the most look-ups a Pulse turn gets: write the message with what you have."
DAILY_CAP_BLOCK = "Pulse has used today's {label} look-ups: write the message without it."
BRIDGE_BLOCK = "Pulse turns call tools directly: call {tool} itself instead of through tool_call."


@dataclass(frozen=True)
class Turn:
    """The Pulse turn a call is made on: its attempt's kind, the kind of
    the item a follow-up is about, the look-ups it has made, today's
    look-ups by label, and whether it may read where the user is
    (pulse_places.reads: Where you are on, and sharing in the background)."""
    kind: str
    item_kind: str = ""
    calls: int = 0
    today: Mapping[str, int] = field(default_factory=dict)
    where: bool = False

    @property
    def searches(self) -> bool:
        return self.kind == FOLLOWUP and self.item_kind in SEARCH_ITEM_KINDS


@dataclass(frozen=True)
class Verdict:
    """``allowed``, else ``message`` says why not; ``labels``: the look-ups
    an allowed call makes, one per call (several through tool_call), none
    for listing tools. ``propose``: a calendar add, which never runs and is
    kept as a suggestion instead (pulse_proposals)."""
    allowed: bool
    message: str = ""
    labels: tuple[str, ...] = ()
    propose: bool = False


def uses(turn: Turn) -> str:
    """What ``turn`` may look things up with, for its note and a block."""
    return (WHERE_USES if turn.where else USES) + (SEARCH_USE if turn.searches else "")


def look_note(turn: Turn) -> str:
    """The part of a Pulse turn's note that says what it may look up and suggest."""
    return LOOK_NOTE.format(limit=MAX_PER_TURN, uses=uses(turn), proposals=MAX_PROPOSALS)


def check(tool_name: Any, args: Any, turn: Turn,
          uses_where: Optional[Callable[[dict], bool]] = None) -> Verdict:
    """Whether a Pulse turn may make this call. ``uses_where(args)``: whether
    a Maps call needs where the user is ("me" or a saved place); Maps' own
    reading of its places by default."""
    if turn.kind not in LOOK_KINDS:
        return Verdict(False, NO_TOOLS)
    name = str(tool_name or "")
    if name in BRIDGE_READS:
        return Verdict(True)
    if name == BRIDGE_CALL:
        return _bridged(args, turn, uses_where)
    return _one(name, args if isinstance(args, dict) else {}, turn, uses_where)


def _one(name: str, args: dict, turn: Turn, uses_where: Optional[Callable[[dict], bool]]) -> Verdict:
    entry = ALLOWED.get(name)
    action = args.get("action") if isinstance(args.get("action"), str) else ""
    if name == "conduit_calendar" and action in ADDS:
        return Verdict(False, propose=True)
    if entry is None or (name == "web_search" and not turn.searches):
        if name == "web_search":
            return Verdict(False, SEARCH_BLOCK)
        return Verdict(False, NOT_ALLOWED.format(what=name or "that tool", uses=uses(turn)))
    if name == "conduit_location" and not turn.where:
        return Verdict(False, LOCATION_BLOCK)
    if entry.actions and action not in entry.actions:
        return Verdict(False, NOT_ALLOWED.format(what=f"{name} {action}" if action else f"{name} without an action",
                                                 uses=uses(turn)))
    # The caps come before Maps reads its places for a call.
    if turn.calls + 1 > MAX_PER_TURN:
        return Verdict(False, TURN_CAP_BLOCK)
    cap = DAILY.get(entry.label)
    if cap is not None and turn.today.get(entry.label, 0) >= cap:
        return Verdict(False, DAILY_CAP_BLOCK.format(label=entry.label))
    if name == "web_search":
        query = args.get("query")
        if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY:
            return Verdict(False, QUERY_BLOCK)
        if any(_ADDRESS.search(text) for text in _texts(args)):
            return Verdict(False, ADDRESS_BLOCK)
    if name == "conduit_maps" and not turn.where and (uses_where or maps_uses_where)(args):
        return Verdict(False, WHERE_BLOCK)
    return Verdict(True, labels=(entry.label,))


def _texts(value: Any) -> list[str]:
    """Every string in a call's arguments, nested ones too."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _texts(item)]
    if isinstance(value, (list, tuple)):
        return [text for item in value for text in _texts(item)]
    return []


def _bridged(args: Any, turn: Turn, uses_where: Optional[Callable[[dict], bool]]) -> Verdict:
    """A tool_call Hermes couldn't unwrap: every call in it must be allowed,
    and all of them count."""
    calls = called(args)
    if not calls:
        return Verdict(False, NOT_ALLOWED.format(what="tool_call", uses=uses(turn)))
    labels: list[str] = []
    today = dict(turn.today)
    for name, arguments in calls:
        if name == BRIDGE_CALL or name in BRIDGE_READS:
            return Verdict(False, BRIDGE_BLOCK.format(tool=name))
        verdict = _one(name, arguments, Turn(turn.kind, turn.item_kind, turn.calls + len(labels), today, turn.where),
                       uses_where)
        if not verdict.allowed:
            if verdict.propose:
                return Verdict(False, BRIDGE_BLOCK.format(tool=name))
            return verdict
        for label in verdict.labels:
            today[label] = today.get(label, 0) + 1
        labels.extend(verdict.labels)
    return Verdict(True, labels=tuple(labels))


def called(args: Any) -> list[tuple[str, dict]]:
    """The calls a tool_call makes, as Hermes reads them (room_rules.
    _called_names): ``calls`` as a list, one call or JSON text, else the
    single ``name``. Both are read, as there, so neither slips by: a call
    Hermes wouldn't run is only checked and counted too. Each comes with its
    arguments (JSON text read too; arguments that can't be read are none, so
    the tool's action check refuses it)."""
    if not isinstance(args, dict):
        return []
    calls = args.get("calls")
    if isinstance(calls, str):
        try:
            calls = json.loads(calls)
        except (ValueError, RecursionError):
            calls = None
    if isinstance(calls, dict):
        calls = [calls]
    entries = [*(calls if isinstance(calls, list) else ()), args]
    found = []
    for entry in entries:
        if not isinstance(entry, dict) or not str(entry.get("name") or "").strip():
            continue
        arguments = entry.get("arguments", entry.get("args"))
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (ValueError, RecursionError):
                arguments = None
        found.append((str(entry["name"]).strip(), arguments if isinstance(arguments, dict) else {}))
    return found


def maps_uses_where(args: dict) -> bool:
    """Whether a Maps call needs where the user is: "me" (the default for
    "near" and "from") or a saved place, as conduit_maps reads its places.
    One Maps refuses anyway for its own reasons is let through to say so,
    unless that reason is where the user is (paused sharing, a private
    place). Fails closed: a location store that can't be read counts."""
    from . import maps_tool

    try:
        location_store = maps_tool._store()
        view = location_store.snapshot() or {"places": {}, "sharing": {}}
        try:
            request = maps_tool._request(str(args.get("action") or ""), args, view, location_store.clock())
        except maps_tool._Refused as refused:
            return refused.answer.get("error") in ("paused", "private")
        return maps_tool._uses_me(request) or maps_tool._uses_saved(request)
    except Exception:  # noqa: BLE001 — unreadable: held to the rule
        logger.debug("conduit_push: Pulse couldn't read Maps' places", exc_info=True)
        return True
