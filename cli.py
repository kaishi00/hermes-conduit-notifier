"""`hermes conduit-push` pairing and diagnostics commands."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import socket
import sqlite3
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import e2e, session_titles
from .client import DEFAULT_RELAY_URL, RelayRejected, UnpairedLocally, build_outgoing, claim_pairing, e2e_status, load_state, post_event, record_relay_accepted, record_relay_refusal, request_key_rotation, send_now, set_redact_content, state_path, unpair
from .relay_urls import check_request_url
from .events import event_id, plugin_hello, push_event


def register_cli(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="conduit_push_action")
    pair = commands.add_parser("pair", help="Pair this Hermes profile with a Conduit device")
    pair.add_argument("code")
    pair.add_argument("--relay-url", default=DEFAULT_RELAY_URL)
    pair.add_argument("--name", default="")
    commands.add_parser("status", help="Show pairing status without revealing credentials")
    test = commands.add_parser("test", help="Send a test notification to the paired device")
    test.add_argument(
        "--corrupt",
        action="store_true",
        help="Send an encrypted test push with a broken ciphertext; the phone must show only generic text",
    )
    commands.add_parser("unpair", help="Revoke this profile's relay credential")
    redact = commands.add_parser(
        "redact",
        help="Redact chat text from pushes; choice labels still transit so cards stay answerable",
    )
    redact.add_argument("mode", choices=["on", "off"])
    encryption = commands.add_parser(
        "e2e",
        help="End-to-end encryption of notifications: rotate asks the phone for a new key on its next connect",
    )
    encryption.add_argument("e2e_action", choices=["rotate"])
    encryption.add_argument(
        "--now",
        action="store_true",
        help="For a key that may have leaked: drop the old key as soon as the new one arrives, with no overlap "
             "(pushes already on their way then show generic text). The current key keeps working until Conduit "
             "next connects to this dashboard; `hermes conduit-push unpair` cuts it off now",
    )
    pulse = commands.add_parser("pulse", help="Pulse, the proactivity experiment: on, off, status, probe, report, home, outcomes, memory, followups, "
                                                 "cards, rate, done, learned, nudge, budget, candidates, note, quiet, night, "
                                                 "dates, ahead, inbox, look, where, private, claims, spots")
    pulse.add_argument("pulse_action", choices=["on", "off", "status", "probe", "report", "home", "outcomes", "memory",
                                                "followups", "cards", "rate", "done", "learned", "nudge", "budget",
                                                "candidates", "note", "quiet", "night", "dates", "ahead", "inbox",
                                                "look", "where", "private", "claims", "spots"])
    pulse.add_argument("value", nargs="?", default="",
                       help="for home: the id of the chat to use, or 'auto' to find it by title; "
                            "for outcomes: how many to show, or a chat id; "
                            "for report: 'now', 'on', 'off', times like 08:00,13:00, a time zone like Europe/Paris, "
                            "'all-profiles' or 'this-profile'; "
                            "for memory: a kind to list, 'now', 'on', 'off', 'forget' and an item number, "
                            "or 'backfill'; "
                            "for followups: 'on', 'off', 'now' (check everything now, for testing), or nothing to list them; "
                            "for rate and done: the card number; "
                            "for learned: 'undo' and a lesson number, 'reset' and a preference number or "
                            "'everything' (what learning holds about it is forgotten), or a kind and a loudness to set; "
                            "for nudge: gentle, balanced or eager; "
                            "for budget: memory or followups; "
                            "for candidates: how many reports to show, or 'bring' and a candidate number "
                            "(one a report left out comes up on the Pulse screen); "
                            "for note: what you want from Pulse, in your own words, or 'clear'; "
                            "for quiet: 'today', 'until' and a day, 'off', 'days' and weekdays like sat,sun "
                            "(or none), or nothing to show the quiet times; "
                            "for night: when it starts, like 22:00, or 'off', or nothing to show it; "
                            "for ahead: 'now' (a look ahead at the next memory run), 'on', 'off', or nothing to "
                            "show the latest; "
                            "for inbox: 'on' or 'off' (off forgets the deadlines it kept), or nothing to show the "
                            "deadlines Pulse kept from the Shortcuts inbox; "
                            "for look: 'on' or 'off' (whether Pulse's turns look things up first and suggest "
                            "adds), or nothing to show today's look-ups, what recent turns looked up and what "
                            "they suggested; "
                            "for where: 'on' or 'off' (whether Pulse reads where you are), or nothing to show what "
                            "waits for a place and what your arrivals and departures did today; "
                            "for private: 'on' or 'off' (whether a push about a gift, a surprise, someone's "
                            "birthday, health or money hides its words on the Lock Screen), or nothing to show it; "
                            "for claims: nothing to list what Hermes said it did with no call behind it, or 'all' "
                            "for every claim with the call that backed it; "
                            "for spots: 'on' or 'off' (whether the phone watches shops for what Pulse held for one), "
                            "or nothing to list what it watches and the shops you came near, with how late iOS told it")
    pulse.add_argument("extra", nargs="?", default="",
                       help="for memory forget: the item number; for memory backfill: how many days back "
                            "(it shows what that costs), 'start', 'stop' or 'cancel'; "
                            "for rate: 'up', 'down', or a reason for a thumbs down (not_relevant, already_knew, "
                            "too_late, too_often, wrong_time); for done: 'undo' to take the mark off; "
                            "for night: when it ends, like 07:00; for learned: the lesson number, "
                            "the preference number (or 'everything') to reset, or push, screen, brief or off; "
                            "for candidates bring: the candidate number; "
                            "for budget: tokens a day, or 'auto' for follow-ups to go with the nudge level; "
                            "for note: more words of the note (a quoted note needs none)")
    prefs = commands.add_parser(
        "prefs",
        help="Conduit's synced preferences: export them to a file, or import one from another host",
    )
    prefs.add_argument("prefs_action", choices=["show", "export", "import"])
    prefs.add_argument("file", nargs="?", default="",
                       help="for export: where to write (prints to the terminal without one); for import: the file to read")
    prefs.add_argument("--replace", action="store_true",
                       help="for import: drop the preferences the file doesn't list, instead of keeping them")
    rooms = commands.add_parser(
        "rooms",
        help="Group rooms with other people's agents: what this profile's agent may do in them (show, set), "
             "deleting its hidden chat for one (forget), setting this Hermes up for them (setup), and rooms with "
             "people you've linked: make one (invite), see yours (drafts), start one now (start), take it back "
             "(withdraw) or take one of their agents out (remove), and see the ones you're invited to (invites), "
             "join or decline one, or leave one; what your agents' turns in rooms other people host wait for "
             "your approval for (approvals), to approve once or deny; and the copies this Hermes keeps of rooms "
             "other people host (copies)",
    )
    rooms.add_argument("rooms_action", choices=["show", "set", "forget", "setup", "invite", "drafts", "start",
                                                "withdraw", "invites", "join", "decline", "leave", "remove",
                                                "approvals", "approve", "deny", "copies"])
    rooms.add_argument("--room", default="",
                       help="the room's id; without one, show and set use the defaults for rooms with no choices "
                            "of their own, and copies lists them all (forget, start, withdraw, join, decline, leave "
                            "and remove need one)")
    rooms.add_argument("--yes", action="store_true",
                       help="for forget, setup, withdraw, leave and remove: go ahead without asking first")
    rooms.add_argument("--name", help="for invite: the room's name")
    rooms.add_argument("--link", action="append", default=None, metavar="LINK",
                       help="for invite: a Link to invite the person of (links list shows them); repeat it for "
                            "more; for join, decline and leave: the Link that invited you, when two did")
    rooms.add_argument("--agent", action="append", default=None, metavar="PROFILE[=HANDLE]",
                       help="for invite and join: one of your agents to bring, by its profile, with its @handle "
                            "in the room when it isn't the one from its bot's name; repeat it for more "
                            "(without one, this profile's); for leave: one of your agents to take out, by its "
                            "profile (without one, all of them)")
    rooms.add_argument("--member", default="", metavar="@HANDLE",
                       help="for remove: the agent to take out of your room, by its @handle or member id "
                            "(rooms drafts shows them)")
    rooms.add_argument("--run", default="", metavar="RUN",
                       help="for approve and deny: the turn waiting for your approval (rooms approvals shows them)")
    rooms.add_argument("--request", default="", metavar="REQUEST",
                       help="for approve and deny: the approval it waits for (rooms approvals shows them)")
    rooms.add_argument("--ceiling", metavar="TOOLSET,...",
                       help="for setup: the toolsets rooms may use on this profile, comma-separated (empty: none); "
                            "without it, the ones saved before, or web, vision, X search and past-chat search")
    rooms.add_argument("--no-restart", dest="no_restart", action="store_true",
                       help="for setup: don't restart the gateway; say how to")
    rooms.add_argument("--tools", choices=["talk", "look", "data", "custom"],
                       help="talk: no tools; look: web lookups; data: lookups and the data tools you pick; "
                            "custom: only the tools you pick")
    rooms.add_argument("--tool", action="append", default=None, metavar="NAME",
                       help="a tool to allow with --tools custom or data; repeat it for more (replaces the list)")
    rooms.add_argument("--share", choices=["helps", "ask", "nothing"],
                       help="what it may share about you: what helps, ask you first, or nothing personal")
    rooms.add_argument("--search", choices=["on", "off"], help="whether it may search your past chats")
    rooms.add_argument("--takes-part", dest="takes_part", choices=["joins", "asked", "listens"],
                       help="joins in, only when asked, or listens only")
    rooms.add_argument("--limit", type=int, help="its turns a day in the room")
    rooms.add_argument("--note", help="a private note for your agent in the room; empty clears it")
    links = commands.add_parser(
        "links",
        help="Links between this Hermes and other people's, for rooms with their agents: make an invite, "
             "preview or accept one, list them, remove a Link or cancel an invite",
    )
    links.add_argument("links_action", choices=["invite", "preview", "accept", "list", "remove", "cancel"])
    links.add_argument("target", nargs="?", default="",
                       help="for preview and accept: the invite's conduit://link/ URL; for remove: the Link's id; "
                            "for cancel: the invite's id")
    links.add_argument("--name", default="",
                       help="for invite: who the other person sees it's from; for accept: who they see linked")
    links.add_argument("--yes", action="store_true", help="for remove: unlink without asking first")
    parser.set_defaults(func=dispatch)


# What a relay request or a bad saved pairing can raise: printed as one line.
_RELAY_ERRORS = (RuntimeError, OSError, ValueError, LookupError, http.client.HTTPException)


def _printable(value: object) -> str:
    return "".join(c for c in str(value) if c.isprintable())


def _block_operator(relay_url: str) -> str:
    """Who can lift a block in front of the relay."""
    if relay_url.rstrip("/") == DEFAULT_RELAY_URL:
        return "email support@milim.dev with this host's public IP address"
    return "ask whoever runs the relay"


def dispatch(args: argparse.Namespace) -> int:
    action = getattr(args, "conduit_push_action", None)
    if action == "pair":
        try:
            state = claim_pairing(args.code, args.relay_url, args.name)
        except _RELAY_ERRORS as error:
            print(f"Pairing failed: {error}")
            if getattr(error, "detail", "") == "gateway_limit_reached":
                print("The relay holds too many pairings for that phone. Turn notifications off and on again in "
                      "Conduit's Settings > Notifications to start fresh, then pair with a new code.")
            if getattr(error, "edge_blocked", False):
                print("That empty 403 came from something in front of the relay blocking this host, so pairing "
                      f"can't get through. To get it lifted, {_block_operator(str(args.relay_url or DEFAULT_RELAY_URL))}.")
            return 1
        print(f"Paired {state['gateway_name']} with Conduit.")
        try:
            send_now(plugin_hello(), timeout=4.0)
        except Exception as error:
            # Pairing succeeded; a failed announcement only means the app's
            # compatibility view fills in on the first real event instead.
            print(f"Warning: could not announce plugin version: {error}")
        return 0
    if action == "status":
        state = load_state()
        if not state:
            print("This Hermes profile is not paired with Conduit.")
            return 1
        print(f"Paired: {_printable(state.get('gateway_name') or socket.gethostname())}")
        relay_url = str(state.get('relay_url') or DEFAULT_RELAY_URL)
        print(f"Relay: {_printable(relay_url)}")
        try:
            check_request_url(f"{relay_url.rstrip('/')}/v1/events")
        except ValueError:
            print("Warning: this is not a usable https:// relay URL, so notifications can't be sent. "
                  "Pair again from Conduit, or run `hermes conduit-push unpair`.")
        if state.get("relay_rejected_at"):
            print(f"Warning: the relay refused this pairing's credential "
                  f"(since {_printable(state['relay_rejected_at'])}), so notifications aren't arriving. "
                  "Conduit removed this gateway or the relay lost its data: pair again from Conduit.")
        if state.get("relay_blocked_at"):
            print(f"Warning: something in front of the relay (a firewall or rate limiter) has been answering this host "
                  f"with an empty 403 since {_printable(state['relay_blocked_at'])}, so notifications, calls and "
                  "Lock Screen progress aren't arriving. Pairing again won't help: it meets the same block. "
                  f"To get it lifted, {_block_operator(relay_url)}.")
        print(f"Redact content: {'on' if state.get('redact_content') else 'off'}")
        encryption = e2e_status(state)
        if encryption["enabled"]:
            origin = {
                "mlkem768+x25519": "quantum-safe, from an ML-KEM-768 + X25519 rotation",
                "x25519": "from an X25519 rotation",
            }.get(encryption["suite"], "handed over through the dashboard")
            print(f"End-to-end encryption: on (key {encryption['kid'][:8]}…, "
                  f"since {_printable(encryption['created_at'] or 'an unknown date')}, {origin})")
            if encryption["previous_kid"]:
                print(f"Previous key {encryption['previous_kid'][:8]}… still opens answers for up to 7 days.")
            if encryption["rotate_requested"] and encryption["rotate_without_overlap"]:
                print("Key rotation: asked for with --now; Conduit hands over a new key the next time it connects to "
                      "this dashboard, and the current key stops opening answers then.")
            elif encryption["rotate_requested"]:
                print("Key rotation: asked for; Conduit hands over a new key the next time it connects to this dashboard.")
            elif encryption["rotation_due"]:
                print("Key rotation: due (the key is over 7 days old); Conduit hands over a new key the next time "
                      "it connects to this dashboard.")
        elif not encryption["crypto"]:
            print("End-to-end encryption: off (this host is missing the cryptography package)")
        else:
            print("End-to-end encryption: off (open Conduit > Settings > Notifications to turn it on)")
        print(f"State: {state_path()}")
        return 0
    if action == "test":
        event = push_event(
            "response.ready",
            identifier=event_id("test"),
            profile=_profile_name(),
            profile_name=session_titles.bot_name(),
            title="Hermes Conduit",
            body="Push notifications are connected.",
        )
        if getattr(args, "corrupt", False):
            state = load_state()
            if not state:
                print("This Hermes profile is not paired with Conduit.")
                return 1
            outgoing = build_outgoing(event, state)
            if "e2e" not in outgoing:
                print("End-to-end encryption is off for this profile; nothing to corrupt.")
                return 1
            outgoing["e2e"]["ct"] = _flipped(outgoing["e2e"]["ct"])
            try:
                post_event(outgoing)
            except _RELAY_ERRORS as error:
                if isinstance(error, RelayRejected) and error.status in (401, 403):
                    record_relay_refusal(state, error)
                print(f"The Conduit relay did not accept the test notification: {error}")
                return 1
            record_relay_accepted(state)
            print("Broken encrypted test notification accepted by the Conduit relay. The phone should show only generic text.")
            return 0
        try:
            send_now(event)
        except _RELAY_ERRORS as error:
            print(f"The Conduit relay did not accept the test notification: {error}")
            if getattr(error, "edge_blocked", False):
                relay_url = str((load_state() or {}).get("relay_url") or DEFAULT_RELAY_URL)
                print("That empty 403 came from something in front of the relay blocking this host, not from the "
                      f"relay, so pairing again won't help. To get it lifted, {_block_operator(relay_url)}.")
            return 1
        print("Test notification accepted by the Conduit relay.")
        return 0
    if action == "redact":
        if not set_redact_content(args.mode == "on"):
            print("This Hermes profile is not paired with Conduit.")
            return 1
        print(
            "Notification content is redacted; approval and clarify cards use generic text."
            if args.mode == "on"
            else "Notification content is no longer redacted."
        )
        return 0
    if action == "e2e":
        state = load_state()
        if not state:
            print("This Hermes profile is not paired with Conduit.")
            return 1
        try:
            requested = request_key_rotation(without_overlap=args.now)
        except _RELAY_ERRORS as error:
            print(f"Couldn't ask for a new key: {error}")
            return 1
        if not requested:
            print("This pairing has no encryption key yet: open Conduit > Settings > Notifications to turn it on.")
            return 1
        print("Asked Conduit for a new encryption key. It hands one over the next time it connects to this "
              "dashboard; until then notifications stay sealed with the current key.")
        if not args.now:
            print("The current key still opens answers for 7 days after the new one arrives, so nothing in "
                  "flight is lost. For a key that may have leaked, use `--now`.")
        else:
            print("The current key stops working as soon as the new one arrives, with no overlap. Until Conduit "
                  "connects it still seals notifications; to cut it off right away, run `hermes conduit-push unpair`.")
        return 0
    if action == "unpair":
        try:
            revoked = unpair()
        except UnpairedLocally as error:
            print(error)
            return 0
        except _RELAY_ERRORS as error:
            print(f"Unpairing failed: {error}")
            return 1
        print("Conduit pairing revoked." if revoked else "This Hermes profile was not paired.")
        return 0
    if action == "pulse":
        return _pulse(args)
    if action == "prefs":
        return _prefs(args)
    if action == "rooms":
        return _rooms(args)
    if action == "links":
        return _links(args)
    print("Usage: hermes conduit-push {pair|status|test|redact|e2e|unpair|pulse|prefs|rooms|links}")
    return 2


def _pulse_model_line(pulse: Any, pulse_store: Any, home: Path, chat_id: str) -> str:
    """The model Pulse's turns run on, and whether its memory, report lists and
    follow-up checks run on it too or on a model Hermes's "Pulse memory" slot
    names."""
    def named(model: dict) -> str:
        return model["model"] + (f" on {model['provider']}" if model["provider"] else "") if model["model"] \
            else f"{model['provider']}'s default model"

    picked = pulse.turn_model(pulse_store)
    own = None if picked else pulse.chat_model(home, chat_id)
    turns = (f"{named(picked)}, from Settings > Pulse" if picked else f"{named(own)}, the Pulse chat's own" if own
             else "Hermes's main model")
    slot = pulse.memory_slot()
    if slot is None and own and not own["provider"]:
        # A chat that names no provider lends Pulse none (pulse.chat_route).
        return f"Model: {turns}, for its turns; memory, report lists and follow-up checks run on Hermes's main model"
    if slot is None or slot == (picked or own):
        return f"Model: {turns}, for its turns, memory, report lists and follow-up checks"
    return (f"Model: {turns}, for its turns; memory, report lists and follow-up checks run on {named(slot)}, "
            "Hermes's \"Pulse memory\" model")


def _pulse(args: argparse.Namespace) -> int:
    from hermes_constants import get_hermes_home

    from . import pulse, pulse_claims, pulse_delivery, pulse_learning, pulse_memory
    home = get_hermes_home()
    action = args.pulse_action
    # Follow-ups are on by default, so even `followups on|off` needs no file yet.
    listing = action in ("followups", "cards", "rate", "done", "learned", "candidates", "dates", "claims") \
        or (action in ("memory", "ahead", "inbox", "look", "where", "private", "spots")
            and (args.value or "").strip() not in ("on", "off")) \
        or (action == "nudge" and not (args.value or "").strip()) \
        or (action == "note" and (args.value or "").strip() in ("", "clear")) \
        or (action in ("quiet", "night") and not (args.value or "").strip()) \
        or (action == "budget" and not str(getattr(args, "extra", "") or "").strip())
    if (action in ("status", "probe", "off", "outcomes") or listing) and not (Path(home) / pulse_delivery.store.DB_NAME).exists():
        # Never turned on: say so without leaving a file behind.
        print("Pulse: off (never turned on). `hermes conduit-push pulse on` to start it."
              + (" Nothing was changed." if action in ("followups", "learned") and (args.value or "").strip() else ""))
        return 1 if action in ("probe", "rate", "done") or (action == "learned" and (args.value or "").strip()) \
            or (action == "candidates" and (args.value or "").strip() == "bring") \
            or (action in ("memory", "ahead") and (args.value or "").strip() in ("now", "forget")) \
            or (action == "followups" and (args.value or "").strip() in ("on", "now")) else 0
    pulse_store = pulse.open_store(home)
    if action in ("on", "off"):
        pulse_store.set("enabled", action == "on")
        if action == "on":
            print("Pulse is on. The dashboard sends the first delivery check within a minute; "
                  "see `hermes conduit-push pulse status`.")
            if pulse_store.get("memory"):
                print("It also reads chats once they've been quiet for 20 minutes and keeps what's worth remembering, "
                      "using Hermes's \"Pulse memory\" auxiliary model. `hermes conduit-push pulse memory off` stops that.")
        else:
            pulse_store.clear_moments()  # no tick runs to prune them
            pulse_claims.stop(pulse_store)  # nor does the ledger record
            print("Pulse is off. Nothing more goes into the home chat.")
        return 0
    if action == "probe":
        if not pulse_store.get("enabled"):
            print("Pulse is off: `hermes conduit-push pulse on` first.")
            return 1
        pulse_store.set("probe_requested", True)
        print("Asked the dashboard for a delivery check; it goes out within a minute.")
        return 0
    if action == "home":
        value = (args.value or "").strip()
        if not value:
            print("Usage: hermes conduit-push pulse home <chat id>|auto")
            return 2
        pulse_store.set("home_session_key", "" if value == "auto" else value)
        print("Pulse finds its chat by title again." if value == "auto" else f"Pulse speaks in {_printable(value)}.")
        return 0
    if action == "outcomes":
        return _pulse_outcomes(pulse_store, (args.value or "").strip())
    if action == "report":
        return _pulse_report(pulse_store, (args.value or "").strip())
    if action == "followups":
        return _pulse_followups(pulse_store, home, (args.value or "").strip())
    if action in ("cards", "rate", "done", "learned"):
        return _pulse_learning(pulse_store, action, (args.value or "").strip(),
                               str(getattr(args, "extra", "") or "").strip(), home)
    if action == "quiet":
        return _pulse_quiet(pulse_store, home, (args.value or "").strip(),
                            str(getattr(args, "extra", "") or "").strip())
    if action == "night":
        return _pulse_night(pulse_store, home, (args.value or "").strip(),
                            str(getattr(args, "extra", "") or "").strip())
    if action == "note":
        # An unquoted two-word note is still the whole note.
        words = ((args.value or "").strip(), str(getattr(args, "extra", "") or "").strip())
        return _pulse_note(pulse_store, " ".join(word for word in words if word))
    if action == "nudge":
        return _pulse_nudge(pulse_store, (args.value or "").strip())
    if action == "candidates":
        return _pulse_candidates(pulse_store, home, (args.value or "").strip(),
                                 str(getattr(args, "extra", "") or "").strip())
    if action == "budget":
        return _pulse_budget(pulse_store, (args.value or "").strip(), str(getattr(args, "extra", "") or "").strip())
    if action == "memory":
        return _pulse_memory(pulse_store, home, (args.value or "").strip(), str(getattr(args, "extra", "") or "").strip())
    if action == "dates":
        return _pulse_dates(pulse_store, home)
    if action == "ahead":
        return _pulse_ahead(pulse_store, home, (args.value or "").strip())
    if action == "inbox":
        return _pulse_inbox(pulse_store, home, (args.value or "").strip())
    if action == "look":
        return _pulse_look(pulse_store, home, (args.value or "").strip())
    if action == "where":
        return _pulse_where(pulse_store, home, (args.value or "").strip())
    if action == "private":
        return _pulse_private(pulse_store, (args.value or "").strip())
    if action == "claims":
        return _pulse_claims(pulse_store, home, (args.value or "").strip())
    if action == "spots":
        return _pulse_spots(pulse_store, home, (args.value or "").strip())
    print(f"Pulse: {'on' if pulse_store.get('enabled') else 'off'}")
    chat, problem = pulse.home_chat(pulse_store, home)
    if chat is not None:
        print(f"Home chat: {_printable(chat.title)} ({_printable(chat.chat_id)})"
              f"{'' if chat.pinned else ', not pinned'}")
        print(_printable(_pulse_model_line(pulse, pulse_store, home, chat.chat_id)))
    if problem:
        print(f"Warning: {_printable(problem)}")
    print(f"Delivery check every {pulse_store.get('probe_interval_hours')} hours")
    if pulse_store.get("enabled"):
        ticked, now = pulse_store.get("last_tick_at"), time.time()
        if isinstance(ticked, bool) or not isinstance(ticked, (int, float)) or now - ticked > pulse.STALE_TICK_S:
            print(f"Warning: the dashboard hasn't run Pulse in the last {int(pulse.STALE_TICK_S // 60)} minutes. "
                  "Check that it's running this build and was restarted after `plugins install`.")
        else:
            print(f"Dashboard last checked: {max(0, int(now - ticked))} s ago")
            lease = pulse_store.get(pulse_delivery.store.LEADER)
            if pulse.lease_on_older_build(lease):
                print(f"Warning: Pulse is run by process {_printable(str(lease.get('owner')))}, which loaded an older build of the "
                      "plugin. Restart it (or the app that started it) so the new build takes over.")
    counts = pulse_store.counts(pulse.PROBE)
    if counts:
        print("Checks so far: " + ", ".join(f"{n} {state}" for state, n in sorted(counts.items())))
    recent = pulse_store.recent(10)
    if recent:
        print("Latest:")
        for row in recent:
            print("  " + _printable(pulse_delivery.describe(row)))
    zone = pulse.report_zone(pulse_store, home)
    if pulse_store.get("daily_report"):
        upcoming = pulse.report.next_time(time.time(), zone, pulse_store.get("report_time"))
        print(f"Shadow reports at {pulse.report.clock_text(pulse_store.get('report_time'))} ({zone}); next "
              + datetime.fromtimestamp(upcoming, zone).strftime("%a %d %b %H:%M"))
    else:
        print("Shadow reports: off")
    if pulse_store.get("enabled"):
        brief_time = pulse_store.get("brief_time") or pulse.brief.DEFAULT_TIME
        upcoming = pulse.report.next_time(time.time(), zone, brief_time)
        print(f"Morning brief at {pulse.report.clock_text(brief_time)} ({zone}); next "
              + datetime.fromtimestamp(upcoming, zone).strftime("%a %d %b %H:%M"))
    else:
        print("Morning brief: off while Pulse is")
    quiet = _quiet_lines(pulse_store, zone, time.time())
    if len(quiet) > 1 or not quiet[0].endswith("none"):
        for line in quiet:
            print(line)
    print(_night_line(pulse_store, zone, time.time()))
    if pulse_store.get("report_profiles") == "all":
        others = [name for name, _ in pulse.report.profile_homes(home)]
        if others:
            print("The report and memory also read profiles: " + ", ".join(_printable(name) for name in others))
    kinds = pulse_store.item_counts()
    usage = pulse_store.get("memory_usage")
    usage = usage if isinstance(usage, dict) else {}
    kept = ", ".join(f"{n} {kind}" for kind, n in sorted(kinds.items()))
    print(("Memory: " if pulse_store.get("memory") else "Memory: off; ")
          + (kept if not kept or pulse_store.get("memory") else "kept " + kept)
          + ("" if kept else "nothing kept yet")
          + f" ({int(usage.get('chunks', 0) or 0)} chat chunks read, {int(usage.get('tokens', 0) or 0)} tokens"
          + (f", {int(usage.get('failed', 0) or 0)} failed" if usage.get("failed") else "")
          + (f", {int(usage.get('skipped', 0) or 0)} skipped after failing" if usage.get("skipped") else "") + ")")
    zone = pulse.report_zone(pulse_store, home)
    backfill = pulse_store.get("memory_backfill")
    if pulse_memory.has_window(backfill):
        back_to = datetime.fromtimestamp(backfill["from"], zone).strftime("%d %b %Y")
        print(f"Backfill: cancelled; memory has read back to {back_to}." if backfill.get("cancelled")
              and pulse_memory.covers_scope(pulse_store, backfill) else
              "Backfill: cancelled; a new backfill reads the other profiles' older chats too."
              if backfill.get("cancelled") else
              "Backfill: read only this profile's older chats; a new backfill reads the others too."
              if backfill.get("done") and not pulse_memory.covers_scope(pulse_store, backfill) else
              f"Backfill: read back to {back_to}." if backfill.get("done") else
              f"Backfill: reading chats back to {back_to}." if backfill.get("active") else
              f"Backfill: paused before reaching {back_to}; `pulse memory backfill start` carries on.")
        stalled = pulse_store.get("memory_backfill_unreadable")
        if backfill.get("active") and isinstance(stalled, list) and stalled:
            print("  It can't read profile(s) " + ", ".join(_printable(name) for name in stalled)
                  + " right now, and finishes once it can.")
    memory_used, memory_cap = pulse._spent_today(pulse_store, "memory", datetime.now(zone).strftime("%Y-%m-%d"))
    if memory_used >= memory_cap:
        print(f"Memory used today's {memory_cap} tokens; it reads again tomorrow.")
    if pulse_store.get("followups") and not pulse_store.get("memory"):
        print("Follow-ups: waiting on memory, which finds Hermes's promises: `hermes conduit-push pulse memory on`")
    elif pulse_store.get("followups"):
        follow = pulse_store.followup_counts()
        usage = pulse_store.get("followup_usage")
        usage = usage if isinstance(usage, dict) else {}
        print("Follow-ups on Hermes's promises and your to-dos: "
              + (", ".join(f"{n} {decision}" for decision, n in sorted(follow.items())) or "none yet")
              + f" ({int(usage.get('checks', 0) or 0)} checks, {int(usage.get('tokens', 0) or 0)} tokens"
              + (f", {int(usage.get('failed', 0) or 0)} failed" if usage.get("failed") else "") + ")")
        follow_used, follow_cap = pulse._spent_today(pulse_store, "followup", datetime.now(zone).strftime("%Y-%m-%d"))
        if follow_used >= follow_cap:
            print(f"Follow-ups used today's {follow_cap} tokens; checks resume tomorrow.")
    else:
        print("Follow-ups: off")
    print(_ahead_line(pulse_store, zone))
    print(_inbox_line(pulse_store, zone))
    print(_look_line(pulse_store))
    print(_where_line(pulse_store, home))
    print(_private_line(pulse_store))
    print(_time_sensitive_line(pulse_store, zone))
    print(f"Nudging: {pulse_learning.nudge(pulse_store)} (`hermes conduit-push pulse nudge gentle|balanced|eager`)")
    note = " ".join(pulse.report.note(pulse_store).split())
    print(f"Note to Pulse: {_printable(note)}" if note
          else "No note to Pulse (`hermes conduit-push pulse note \"what you want from it\"`)")
    learning_lines = _learning_status(pulse_store)
    if learning_lines:
        print(learning_lines)
    ratings = pulse.rating_counts(pulse_store)
    if ratings:
        print("Ratings so far: " + ", ".join(f"{kind} {yes} yes / {no} no" for kind, (yes, no) in ratings.items()))
    outcomes = pulse_store.outcome_counts(time.time() - 86400)
    if outcomes or pulse_store.get("enabled"):
        print("Tool outcomes in the last 24 hours: "
              + (", ".join(f"{n} {status}" for status, n in sorted(outcomes.items())) or "none yet"))
    print(f"State: {pulse_store.path}")
    return 0


def _prefs(args: argparse.Namespace) -> int:
    from hermes_constants import get_hermes_home

    from . import prefs_store

    store = prefs_store.PrefsStore(Path(get_hermes_home()))
    action = args.prefs_action
    path = str(getattr(args, "file", "") or "")
    if action == "show":
        values = store.view()["values"]
        if not values:
            print("No Conduit preferences are synced to this profile yet.")
            return 0
        for key in sorted(values):
            entry = values[key]
            when = datetime.fromtimestamp(entry["at"]).strftime("%Y-%m-%d %H:%M")
            print(f"{key}: {_printable(json.dumps(entry['value'], ensure_ascii=False))[:120]} "
                  f"({when}, {_printable(entry.get('by_name') or 'Conduit')})")
        return 0
    if action == "export":
        exported = store.export()
        encoded = json.dumps(exported, indent=2, ensure_ascii=False) + "\n"
        if not path:
            print(encoded, end="")
            return 0
        try:
            # Private like the store itself.
            descriptor = os.open(Path(path).expanduser(), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            if hasattr(os, "fchmod"):  # an export over an older, wider file
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(encoded)
        except OSError as error:
            print(f"Couldn't write {path}: {error}")
            return 1
        print(f"Exported {len(exported['values'])} preferences to {path}. "
              "Import it on the other host with `hermes conduit-push prefs import <file>`.")
        return 0
    if not path:
        print("Name the file to import: hermes conduit-push prefs import <file> [--replace]")
        return 2
    try:
        document = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
        imported = store.import_(document, replace=bool(getattr(args, "replace", False)))
    except (OSError, ValueError, RecursionError) as error:
        print(f"Couldn't import {path}: {error}")
        return 1
    print(f"Imported. This profile now syncs {len(imported['values'])} Conduit preferences; "
          "Conduit picks them up the next time it connects.")
    return 0


_ROOM_TOOLS = {"talk": "Talk only", "look": "Look things up", "data": "Use my data", "custom": "Custom"}
_ROOM_SHARE = {"helps": "what helps", "ask": "asks you first", "nothing": "nothing personal"}
_ROOM_TAKES_PART = {"joins": "joins in", "asked": "when asked", "listens": "listens only"}


def _rooms(args: argparse.Namespace) -> int:
    from hermes_constants import get_hermes_home

    from . import room_choices

    room = str(getattr(args, "room", "") or "").strip()
    if args.rooms_action in _LINKED_ROOMS:
        return _linked_rooms(args, room, Path(get_hermes_home()))
    if args.rooms_action in _ROOM_APPROVALS:
        return _room_approvals(args, Path(get_hermes_home()))
    if args.rooms_action == "copies":
        from . import room_identity

        return _room_copies(room, room_identity.root_home(Path(get_hermes_home())))
    if args.rooms_action == "forget":
        return _forget_room(room, Path(get_hermes_home()), bool(getattr(args, "yes", False)))
    if args.rooms_action == "setup":
        return _setup_rooms(args, Path(get_hermes_home()))
    store = room_choices.ChoicesStore(Path(get_hermes_home()))
    try:
        if args.rooms_action == "show":
            if room:
                own = store.saved(room)
                _print_room_choices(f"Room {room}" + ("" if own else " (the defaults: no choices of its own)"),
                                    store.get(room))
                return 0
            _print_room_choices("Defaults for rooms with no choices of their own", store.defaults())
            for room_id in sorted(store.view()["rooms"]):
                _print_room_choices(f"Room {room_id}", store.get(room_id))
            return 0
        changes: dict[str, Any] = {}
        for field, value in (("tools", args.tools), ("custom_tools", args.tool), ("share", args.share),
                             ("takes_part", args.takes_part), ("daily_limit", args.limit), ("note", args.note)):
            if value is not None:
                changes[field] = value
        if args.search is not None:
            changes["search_chats"] = args.search == "on"
        if not changes:
            print("Name a choice to set, for example: hermes conduit-push rooms set --room <id> --tools look")
            return 2
        saved = store.set(room, changes) if room else store.set_defaults(changes)
    except (room_choices.ChoicesError, OSError) as error:
        print(f"Couldn't {'set' if args.rooms_action == 'set' else 'show'} that: {error}")
        return 1
    _print_room_choices(f"Room {room}" if room else "Defaults for rooms with no choices of their own", saved)
    return 0


def _forget_room(room: str, home: Path, yes: bool) -> int:
    """Deletes this profile's hidden chat for a room (room_cleanup), as
    `hermes sessions delete` deletes one, after saying what that does."""
    from . import room_cleanup

    if not room:
        print("Name the room: hermes conduit-push rooms forget --room <id>")
        return 2
    try:
        room_cleanup.check_room_id(room)
    except room_cleanup.CleanupError as error:
        print(f"Couldn't delete it: {error}")
        return 1
    print(f"This deletes your agent's hidden chat for room {room} on this profile, with its earlier history and "
          "its helpers' chats. If the room is still going on someone else's Hermes, your agent starts over "
          "there without it on its next turn.")
    if not yes:
        try:
            answer = input("Delete it? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("Cancelled.")
            return 0
    try:
        result = room_cleanup.forget_room(room, home)
    except (room_cleanup.CleanupError, OSError, sqlite3.Error) as error:
        print(f"Couldn't delete it: {error}")
        return 1
    deleted, kept = result["deleted"], result["kept"]
    if deleted:
        print(f"Deleted {deleted} hidden chat{'' if deleted == 1 else 's'} for room {room}.")
    elif not kept:
        print(f"This profile has no hidden chat for room {room}.")
    if kept:
        print(f"Kept {kept}: a turn in it is still finishing. Try again in a minute.")
        return 1
    return 0


def _setup_rooms(args: argparse.Namespace, home: Path) -> int:
    """Sets this Hermes up for rooms with other people's agents (room_setup):
    says what it changes, asks, saves, then restarts the gateway when the
    API server isn't running yet."""
    from . import room_setup

    profile = room_setup.profile_name(home)
    ceiling = None
    if args.ceiling is not None:
        ceiling = [name.strip() for name in str(args.ceiling).split(",") if name.strip()]
    restart = "hermes gateway restart" if profile == "default" else "hermes -p default gateway restart"
    try:
        hermes = room_setup.Hermes.load()
        state = room_setup.state(hermes, home, profile, ceiling=ceiling)
    except room_setup.SetupError as error:
        print(f"Couldn't set it up: {error}")
        return 1
    if state["blockers"]:
        print("This Hermes can't be set up for rooms yet:")
        for blocker in state["blockers"]:
            print(f"  {_printable(blocker['message'])}")
        return 1
    labels = {choice["id"]: choice["label"] for choice in state["choices"]}
    tools = ", ".join(_printable(labels.get(name, name)) for name in state["ceiling"]) or "none: agents only talk"
    server = state["api_server"]
    changes = state["changes"]
    if not state["needs"]:
        print(f"This Hermes is already set up for rooms (profile {profile}): its API server runs at "
              f"{server['address']}, and rooms can use {tools}.")
        return 0
    print(f"Setting this Hermes up for rooms with other people's agents (profile {profile}):")
    if "api_server_key" in changes:
        print(f"- Hermes' API server: turned on with a new key, saved in "
              f"{room_setup.room_identity.root_home(home) / '.env'}, at {server['address']}. Rooms reach your "
              "agents through it.")
    elif "api_server_enabled" in changes or "api_server_port" in changes:
        print(f"- Hermes' API server: turned on with the key it has, at {server['address']}. Rooms reach your "
              "agents through it.")
    else:
        print(f"- Hermes' API server: {'on' if server['running'] else 'set up'}, at {server['address']}.")
    if "profile_key" in changes:
        print(f"- Profile {profile} gets a key of its own for it, saved in {home / '.env'}: the API server lets a "
              "room reach a profile's agent only with that profile's key.")
    if server["error"]:
        print(f"  It didn't start the last time: {_printable(server['error'])}")
    print(f"- Tools rooms can use: {tools}.")
    others = [_printable(choice["label"]) for choice in state["choices"] if choice["id"] not in state["ceiling"]]
    if others:
        print(f"  Others you can add with --ceiling: {', '.join(others)}.")
    if ceiling is not None and "ceiling" in changes \
            and "ceiling" not in room_setup.state(hermes, home, profile)["changes"]:
        # A ceiling the setup saved before: each room pins its members'
        # tools when it starts (designs/cross-owner-agents, decision 13).
        print(f"- Profile {profile}'s agent then stops answering in rooms already started with other people's "
              "agents, which hold it to the tools it had when they started. New rooms use these.")
    print("- Tools added later (a plugin, an MCP server, a Hermes update) stay out of rooms until you add them here.")
    if "approvals" in state["needs"]:
        print("- Approvals: off becomes manual, since rooms need manual or smart approvals.")
    if server["enabled"] and "ceiling" in state["needs"]:
        print("- The API server is already on, so these tools are also all that other apps using it get "
              "(Open WebUI and the like).")
    if "restart" in state["needs"] and not args.no_restart:
        print("- Then the gateway restarts to start the API server.")
    if not args.yes:
        try:
            answer = input("Save this? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("Cancelled.")
            return 0
    try:
        after = room_setup.apply(hermes, home, profile, ceiling=ceiling)
    except (room_setup.SetupError, OSError, ValueError, RuntimeError) as error:
        print(f"Couldn't set it up: {error}")
        return 1
    print("Saved.")
    if "restart" not in after["needs"]:
        return 0
    command = None if args.no_restart else room_setup.restart_command()
    if command is None:
        print(f"Restart the gateway to start the API server, at {after['api_server']['address']}: {restart}")
        return 0
    print("Restarting the gateway to start the API server...")
    code = subprocess.call(command)
    if code != 0:
        print(f"The gateway didn't restart (exit {code}). Restart it yourself: {restart}")
        return 1
    server = _restarted_api_server(room_setup, hermes, home, profile)
    if server["running"]:
        print(f"The gateway restarted, and its API server runs at {server['address']}.")
        return 0
    why = f": {_printable(server['error']).rstrip('.')}" if server["error"] else ""
    print(f"The gateway restarted, but its API server isn't running{why}. See: hermes gateway status")
    return 1


# How long `rooms setup` waits for the restarted gateway to record its API
# server running (or failing).
RESTART_WAIT_S = 20.0


def _restarted_api_server(room_setup: Any, hermes: Any, home: Path, profile: str) -> dict:
    deadline = time.monotonic() + RESTART_WAIT_S
    while True:
        server = room_setup.state(hermes, home, profile)["api_server"]
        if server["running"] or server["error"] or time.monotonic() >= deadline:
            return server
        time.sleep(0.5)


# How long a links command waits for the dashboard's loop to see what it
# changed before saying nothing reads the mailbox: the loop looks every 2 s.
LINKS_READING_WAIT_S = 3.0
_LINK_STATUS = {"linked": "linked", "waiting": "waiting for their Hermes to confirm",
                "unanswered": "never confirmed; link again with a new invite"}
_LINKS_USAGE = {
    "preview": "hermes conduit-push links preview <conduit://link/...>",
    "accept": "hermes conduit-push links accept <conduit://link/...> --name <your name>",
    "remove": "hermes conduit-push links remove <Link id> (links list shows them)",
    "cancel": "hermes conduit-push links cancel <invite id> (links list shows them)",
}


def _links(args: argparse.Namespace) -> int:
    """Links between this installation and other people's (room_links): the
    CLI makes and changes them, and the dashboard's loop reads the mailbox."""
    from hermes_constants import get_hermes_home

    from . import room_identity, room_links

    action = args.links_action
    target = str(getattr(args, "target", "") or "").strip()
    name = str(getattr(args, "name", "") or "").strip()
    if action in _LINKS_USAGE and not target:
        print(f"Usage: {_LINKS_USAGE[action]}")
        return 2
    if action in ("invite", "accept") and not name:
        print(f"Say who the other person sees {'it is from' if action == 'invite' else 'linked'} with --name, "
              f"for example: hermes conduit-push links {action}{' <url>' if action == 'accept' else ''} --name Eric")
        return 2
    root = room_identity.root_home(get_hermes_home())
    links = room_links.Links(root)
    verb = {"invite": "make the invite", "preview": "read that invite", "accept": "accept it", "list": "list them",
            "remove": "unlink", "cancel": "cancel it"}[action]
    try:
        if action == "invite":
            made = links.invite(name)
            print(f"An invite from {_printable(made['invite']['name'])}, for one accept until "
                  f"{_link_time(made['invite']['expires_at'])}:")
            print(made["url"])
            print(f"Your Hermes' words: {' '.join(made['words'])}. The other person should see the same "
                  "four words when they open the invite.")
            _say_if_nothing_reads(room_links, root)
        elif action == "preview":
            shown = links.preview(target)
            print(f"From {_printable(shown['name'])}, through the mailbox at {_printable(shown['mailbox'])}. "
                  f"Their Hermes' words: {' '.join(shown['words'])}.")
            if shown["own"]:
                print("It's your own invite: it's for the person you're linking with.")
            elif shown["expired"]:
                print("It has run out: ask them for a new one.")
            else:
                print(f"It can be accepted until {_link_time(shown['expires_at'])}.")
            if shown["linked_as"]:
                print(f"This Hermes is already linked with them (Link {shown['linked_as']}).")
        elif action == "accept":
            try:
                link = links.accept(target, name)["link"]
            except room_links.AcceptKept as kept:
                print(f"The accept didn't go yet: {kept}.")
                _say_if_nothing_reads(room_links, root, "the accept waits here")
                return 1
            print(f"Linked with {_printable(link['name'])} once their Hermes confirms it (Link {link['id']}).")
            _say_if_nothing_reads(room_links, root)
        elif action == "list":
            view = links.view()
            if not view["links"] and not view["invites"]:
                print("No Links or invites yet. Make an invite: hermes conduit-push links invite --name <your name>")
                return 0
            for link in view["links"]:
                print(f"{_printable(link['name'])}: {_LINK_STATUS.get(link['status'], link['status'])}, words "
                      f"{' '.join(link['words'])} (Link {link['id']})")
            for invite in view["invites"]:
                print(f"Invite from {_printable(invite['name'])}, until {_link_time(invite['expires_at'])} "
                      f"(invite {invite['id']})")
            mailbox = view["mailbox"]
            print(f"Mailbox: {_printable(mailbox['url'])}"
                  + ("" if mailbox["reading"] else "; nothing on this Hermes reads it right now"))
        elif action == "remove":
            return _remove_link(links, target, bool(getattr(args, "yes", False)))
        elif not links.cancel(target)["cancelled"]:
            print(f"No invite {_printable(target)} is waiting on this Hermes.")
            return 1
        else:
            print("Cancelled the invite: it can't be accepted now.")
    except (room_links.LinkError, OSError) as error:
        print(f"Couldn't {verb}: {error}")
        return 1
    return 0


def _remove_link(links: Any, link_id: str, yes: bool) -> int:
    link = links.get(link_id)
    if link is None:
        print(f"No Link {_printable(link_id)} on this Hermes. hermes conduit-push links list shows them.")
        return 1
    who = _printable(link["name"])
    print(f"This unlinks {who}'s Hermes from this one, on both sides. Linking again takes a new invite.")
    if not yes:
        try:
            answer = input("Unlink? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("Cancelled.")
            return 0
    links.remove(link_id)
    print(f"Unlinked {who}. Their Hermes drops the Link when it next reads its mailbox.")
    return 0


def _say_if_nothing_reads(room_links: Any, root: Path,
                          then: str = "the answer waits there, for up to 7 days,") -> None:
    deadline = time.monotonic() + LINKS_READING_WAIT_S
    while not room_links.reading(root):
        if time.monotonic() >= deadline:
            print(f"Nothing on this Hermes reads its rooms mailbox right now, so {then} until Hermes' dashboard "
                  "runs with this plugin.")
            return
        time.sleep(0.2)


# Rooms with people you've linked: what each command does, and how to use the ones that name a room.
_LINKED_ROOMS = {"invite": "make the room", "drafts": "list them", "start": "start it", "withdraw": "take it back",
                 "invites": "list them", "join": "join it", "decline": "decline it", "leave": "leave it",
                 "remove": "take it out"}
_LINKED_ROOMS_USAGE = {
    "start": "hermes conduit-push rooms start --room <id> (rooms drafts shows them)",
    "withdraw": "hermes conduit-push rooms withdraw --room <id> (rooms drafts shows them)",
    "join": "hermes conduit-push rooms join --room <id> [--agent PROFILE[=HANDLE]]... (rooms invites shows them)",
    "decline": "hermes conduit-push rooms decline --room <id> (rooms invites shows them)",
    "leave": "hermes conduit-push rooms leave --room <id> [--agent PROFILE]... (rooms invites shows them)",
    "remove": "hermes conduit-push rooms remove --room <id> --member @HANDLE (rooms drafts shows them)",
}
_DRAFT_ENDED = {"withdrawn": "taken back", "lapsed": "ran out before it started",
                "too_few": "ended: nobody else's agents could join", "start_failed": "ended: Hermes couldn't make it",
                "disbanded": "disbanded"}
_INVITE_ENDED = {"started_without_you": "it started without you", "refused": "their Hermes couldn't reach your agents",
                 "too_few": "nobody else's agents could join", "start_failed": "their Hermes couldn't make it",
                 "withdrawn": "they took it back", "lapsed": "it ran out", "removed": "they took your agents out",
                 "disbanded": "they disbanded it"}


def _linked_rooms(args: argparse.Namespace, room: str, home: Path) -> int:
    """Rooms with people you've linked (room_invites): the CLI makes them and
    answers invites, and the dashboard's rooms loop, which reads the answers,
    starts them."""
    from . import room_identity, room_invites, room_links

    action = args.rooms_action
    member = str(getattr(args, "member", "") or "").strip()
    if (action in _LINKED_ROOMS_USAGE and not room) or (action == "remove" and not member):
        print(f"Usage: {_LINKED_ROOMS_USAGE[action]}")
        return 2
    root = room_identity.root_home(home)
    try:
        rooms = room_invites.here(root)
        if action == "invite":
            return _invite_to_room(rooms, room_invites, room_links, root, home, args)
        if action == "leave":
            return _leave_room(rooms, room, args)
        if action == "remove":
            return _remove_from_room(rooms, room, member, bool(getattr(args, "yes", False)))
        if action == "drafts":
            _print_drafts(rooms.drafts())
        elif action == "start":
            draft = rooms.start(room)
            print(f"{_printable(draft['name'])} starts now with whoever has joined, and the rest are told it "
                  "started without them.")
            _say_if_nothing_reads(room_links, root, "it can't start")
        elif action == "withdraw":
            return _withdraw_room(rooms, room, bool(getattr(args, "yes", False)))
        elif action == "invites":
            from . import room_setup

            _print_invites(rooms.invites(), room_setup.profile_name(home))
        else:
            return _answer_invite(rooms, room_invites, room_links, root, home, room, args)
    except (room_invites.RoomsError, room_links.LinkError, OSError) as error:
        print(f"Couldn't {_LINKED_ROOMS[action]}: {error}")
        return 1
    return 0


# Your agents' approvals in rooms other people host: what each command does.
_ROOM_APPROVALS = {"approvals": "list them", "approve": "approve it", "deny": "deny it"}


def _room_approvals(args: argparse.Namespace, home: Path) -> int:
    """What your agents' turns in rooms other people host wait for your
    approval for (room_approvals), answered here, once or deny."""
    from . import room_approvals, room_identity

    action = args.rooms_action
    run, request = (str(getattr(args, name, "") or "").strip() for name in ("run", "request"))
    if action != "approvals" and not (run and request):
        print(f"Usage: hermes conduit-push rooms {action} --run <run id> --request <request id> (rooms approvals "
              "shows them)")
        return 2
    try:
        approvals = room_approvals.here(room_identity.root_home(home))
        if action == "approvals":
            _print_approvals(approvals.waiting())
        elif action == "approve":
            approvals.answer(run, request, "once")
            print("Approved it, once: the turn goes on.")
        else:
            approvals.answer(run, request, "deny")
            print("Denied it: the turn goes on without it.")
    except (room_approvals.ApprovalsError, OSError) as error:
        print(f"Couldn't {_ROOM_APPROVALS[action]}: {error}")
        return 1
    return 0


def _room_copies(room: str, root: Path) -> int:
    """The copies this Hermes keeps of rooms other people host (room_copies),
    each with who hosts it and how much of it the copy has; with a room, its
    members too."""
    from . import room_copies

    try:
        shown = [room_copies.copy(root, room)] if room else room_copies.copies(root)
    except (room_copies.CopiesError, OSError, sqlite3.Error, ValueError) as error:
        print(f"Couldn't show {'it' if room else 'them'}: {error}")
        return 1
    if not shown:
        print("This Hermes keeps no copies of rooms other people host.")
    for copy in shown:
        held = copy["last_seq"]
        status = _printable(copy["status"] + (f" ({copy['reason']})" if copy["reason"] else ""))
        print(f"{copy['room_id']}: {_printable(copy['name'])}, hosted by {_printable(copy['from'])}, {status}; "
              f"{held} event{'' if held == 1 else 's'} of {copy['latest_seq']}")
        if room:
            for member in copy["members"]:
                owner = _printable(f"{member['owner']}'s") if member["owner"] else "the host's"
                print(f"  @{_printable(member['handle'])} ({_printable(member['display_name'])}, {owner})")
    return 0


def _print_approvals(approvals: list) -> None:
    if not approvals:
        print("None of your agents' turns in rooms other people host waits for your approval.")
    for approval in approvals:
        room = _printable(approval["room"] or "A room")
        host = _printable(approval["from"] or "someone")
        handle = approval["agent"]["handle"] or approval["agent"]["profile"]
        command = _printable(approval["command"])
        print(f"{room}, from {host} (room {approval['room_id']}): @{handle} asks to run {command}")
        if approval["description"]:
            print(f"  {_printable(approval['description'])}")
        answer = f"--run {approval['run_id']} --request {approval['request_id']}"
        print(f"  hermes conduit-push rooms approve {answer} (once), or rooms deny {answer}")


def _invite_to_room(rooms: Any, room_invites: Any, room_links: Any, root: Path, home: Path,
                    args: argparse.Namespace) -> int:
    name = str(getattr(args, "name", "") or "").strip()
    links = list(getattr(args, "link", None) or [])
    if not name or not links:
        print(f"Name the room with --name and the people to invite by their Link with --link (links list shows "
              f"them), for example: hermes conduit-push rooms invite --name \"Zoo trip\" --link <Link id>")
        return 2
    draft = rooms.draft(name, _room_agents(room_invites, root, home, args), links, where={"kind": "any"})
    print(f"Invited {_and(_printable(person['name']) for person in draft['people'])} to "
          f"{_printable(draft['name'])} (room {draft['room_id']}) with {_handles(draft['agents'])}. It starts by "
          "itself once everyone has answered; to start it sooner with whoever has joined: "
          f"hermes conduit-push rooms start --room {draft['room_id']}")
    _say_if_nothing_reads(room_links, root, "their answers wait there, and the room can't start,")
    return 0


def _answer_invite(rooms: Any, room_invites: Any, room_links: Any, root: Path, home: Path, room: str,
                   args: argparse.Namespace) -> int:
    links = getattr(args, "link", None) or []
    link = str(links[-1]).strip() if links else ""
    invites = [invite for invite in rooms.invites() if invite["room_id"] == room and link in ("", invite["link"])]
    if not invites:
        print(f"No invite to room {_printable(room)} on this Hermes. hermes conduit-push rooms invites shows them.")
        return 1
    if len(invites) > 1:
        print(f"Two Links invited you to room {_printable(room)}: say which with --link (rooms invites shows them).")
        return 2
    invite = invites[0]
    host = _printable(invite["from"])
    if args.rooms_action == "decline":
        shown = rooms.answer(invite["link"], room, [])
        print(f"Declined {_printable(shown['name'])}; {host}'s Hermes is told.")
        return 0
    shown = rooms.answer(invite["link"], room, _room_agents(room_invites, root, home, args))
    print(f"Joined {_printable(shown['name'])} with {_handles(shown['joined'])}. It starts on {host}'s Hermes "
          "once everyone has answered.")
    _say_if_nothing_reads(room_links, root, f"{host}'s Hermes can't reach your agents in it")
    return 0


def _withdraw_room(rooms: Any, room: str, yes: bool) -> int:
    draft = next((shown for shown in rooms.drafts()["drafts"] if shown["room_id"] == room), None)
    if draft is None:
        print(f"No room {_printable(room)} of yours on this Hermes. hermes conduit-push rooms drafts shows them.")
        return 1
    name = _printable(draft["name"])
    print(f"This takes back {name}: everyone you invited who hasn't declined is told, and their Hermes undoes "
          "their agents' joins.")
    if not yes:
        try:
            answer = input("Take it back? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("Cancelled.")
            return 0
    rooms.withdraw(room)
    print(f"Took back {name}.")
    return 0


def _leave_room(rooms: Any, room: str, args: argparse.Namespace) -> int:
    """Takes your agents out of a room you joined, after saying what that
    does: those --agent names, else all of them."""
    links = getattr(args, "link", None) or []
    link = str(links[-1]).strip() if links else ""
    invites = [invite for invite in rooms.invites() if invite["room_id"] == room and link in ("", invite["link"])]
    if not invites:
        print(f"No invite to room {_printable(room)} on this Hermes. hermes conduit-push rooms invites shows them.")
        return 1
    if len(invites) > 1:
        print(f"Two Links invited you to room {_printable(room)}: say which with --link (rooms invites shows them).")
        return 2
    invite = invites[0]
    named = getattr(args, "agent", None)
    profiles = [str(spec).partition("=")[0].strip() for spec in named] if named else None
    leaving = [agent for agent in invite["joined"] if profiles is None or agent["profile"] in profiles]
    if invite["status"] == "started" and leaving and len(leaving) == len(profiles or leaving):
        print(f"This takes {_handles(leaving)} out of {_printable(invite['name'])}: {_printable(invite['from'])}'s "
              f"Hermes is told, and {'its' if len(leaving) == 1 else 'their'} turns there stop.")
        if not getattr(args, "yes", False) and not _confirmed("Leave it? [y/N] "):
            print("Cancelled.")
            return 0
    shown = rooms.leave(invite["link"], room, profiles)
    print(f"Left {_printable(shown['name'])} with {_handles(leaving)}. " + (
        "Its hidden chat for the room stays on this Hermes; to delete it:" if len(leaving) == 1
        else "Their hidden chats for the room stay on this Hermes; to delete one:"))
    for agent in leaving:
        named_profile = "" if agent["profile"] == "default" else f"-p {agent['profile']} "
        print(f"  hermes {named_profile}conduit-push rooms forget --room {room}")
    return 0


def _remove_from_room(rooms: Any, room: str, member: str, yes: bool) -> int:
    """Takes another person's agent out of a room you host, after saying
    what that does."""
    draft = next((shown for shown in rooms.drafts()["drafts"] if shown["room_id"] == room), None)
    if draft is None:
        print(f"No room {_printable(room)} of yours on this Hermes. hermes conduit-push rooms drafts shows them.")
        return 1
    members = draft["members"] or []
    handle = member.removeprefix("@").casefold()
    found = next((each for each in members if each["member_id"] == member), None) or next(
        (each for each in members if each["handle"].casefold() == handle), None)
    if found is None:
        # Refused as the plugin refuses it: asking it could take out an agent the room started with meanwhile,
        # unasked.
        why = {"ended": "That room has ended",
               "started": "There's no agent with that handle or id in that room"}.get(
            draft["status"], "That room hasn't started yet")
        print(f"Couldn't {_LINKED_ROOMS['remove']}: {why}")
        return 1
    if found["owner"] is not None and found["status"] == "in":
        owner = _printable(found["owner"])
        print(f"This takes @{found['handle']} ({owner}'s) out of {_printable(draft['name'])}: {owner}'s Hermes is "
              "told, and its turns there stop.")
        if not yes and not _confirmed("Take it out? [y/N] "):
            print("Cancelled.")
            return 0
    rooms.remove(room, member)  # refuses one that isn't a linked person's agent
    took = f"@{found['handle']} ({_printable(found['owner'])}'s) out of {_printable(draft['name'])}"
    print(f"You took {took} already." if found["status"] == "removed" else f"Took {took}.")
    return 0


def _confirmed(prompt: str) -> bool:
    try:
        answer = input(prompt)
    except EOFError:
        answer = ""
    return answer.strip().lower() in ("y", "yes")


def _room_agents(room_invites: Any, root: Path, home: Path, args: argparse.Namespace) -> list:
    """The agents ``--agent`` names (PROFILE or PROFILE=HANDLE), else this
    profile's."""
    from . import room_setup

    agents = []
    for spec in getattr(args, "agent", None) or [room_setup.profile_name(home)]:
        profile, _, handle = str(spec).partition("=")
        agents.append(room_invites.own_agent(root, profile.strip(), handle.strip() or None))
    return agents


def _print_drafts(view: dict) -> None:
    if not view["drafts"]:
        print("No rooms with people you've linked yet. Make one: hermes conduit-push rooms invite --name <name> "
              "--link <Link id>")
    for draft in view["drafts"]:
        if draft["status"] == "waiting":
            status = f"waiting for answers until {_link_time(draft['expires_at'])}"
        elif draft["status"] == "started":
            status = f"started {_link_time(draft['started_at'])}"
            if draft["cut_off"]:
                status += f", with {len(draft['cut_off'])} agent(s) its Hermes can't reach"
        elif draft["status"] == "ended":
            status = _DRAFT_ENDED.get(draft["reason"], "ended")
        else:
            status = "starting"
        print(f"{_printable(draft['name'])} (room {draft['room_id']}): {status}")
        # A started room's handles, which the start made unique: what rooms remove takes.
        handles = {member["member_id"]: member["handle"] for member in draft["members"] or ()}
        for person in draft["people"]:
            agents = [{**agent, "handle": handles.get(agent["member_id"], agent["handle"])}
                      for agent in person["agents"]]
            answered = {"waiting": "hasn't answered", "declined": "declined"}.get(person["status"]) \
                or f"joined with {_handles(agents)}"
            out = _out([agent["handle"] for agent in agents if agent["member_id"] in draft["left"]],
                       [agent["handle"] for agent in agents if agent["member_id"] in draft["removed"]])
            print(f"  {_printable(person['name'])}: {answered}" + (f" ({out})" if out else ""))
    if not view["starts"]["available"]:
        print("This Hermes can't start rooms with people you've linked: update Hermes.")


def _print_invites(invites: list, profile: str) -> None:
    """The join card for each invite; one waiting for an answer shows the
    choices this profile would join with."""
    from . import room_choices

    if not invites:
        print("No rooms you're invited to over a Link.")
    for invite in invites:
        if invite["status"] == "waiting":
            status = f"waiting for your answer until {_link_time(invite['expires_at'])}"
        elif invite["status"] == "joined":
            status = f"joined with {_handles(invite['joined'])}, waiting for it to start"
        elif invite["status"] == "cancelled":
            status = f"not starting with your agents: {_INVITE_ENDED.get(invite['reason'], 'it ended')}"
        elif invite["status"] == "ended":
            status = f"ended: {_INVITE_ENDED.get(invite['reason'], 'it ended')}"
        else:
            status = {"lapsed": "ran out", "declined": "declined", "started": "started",
                      "left": "you left it"}.get(invite["status"], invite["status"])
            out = _out([agent["handle"] for agent in invite["gone"] if agent["reason"] == "left"],
                       [agent["handle"] for agent in invite["gone"] if agent["reason"] == "removed"])
            if invite["status"] == "started" and out:
                status += f"; {out}"
        host = _printable(invite["from"])
        print(f"{_printable(invite['name'])}, from {host} (room {invite['room_id']}, Link {invite['link']}): "
              f"{status}")
        agents = ", ".join(f"{_printable(agent['display_name'])} (@{agent['handle']})" for agent in invite["agents"])
        others = "".join(f"{_printable(person)}, and " for person in invite["people"])
        print(f"  With {others}{host}'s agents: {agents}")
        if invite["status"] == "waiting":
            print(f"  Places for {invite['slots']} of your agents: hermes conduit-push rooms join --room "
                  f"{invite['room_id']} [--agent PROFILE]..., or rooms decline --room {invite['room_id']}")
            mine = next((entry["choices"] for entry in invite["profiles"] if entry["profile"] == profile), None)
            if mine is not None:
                _print_room_choices(f"{profile} would join with", room_choices.choices_from(mine), indent="  ")


def _handles(agents: Any) -> str:
    return _and(f"@{agent['handle']}" for agent in agents) or "none of your agents"


def _out(left: list, removed: list) -> str:
    """The agents that left a room and those taken out, by handle: "@a left, @b taken out"."""
    return ", ".join(f"{_and(f'@{handle}' for handle in handles)} {what}"
                     for handles, what in ((left, "left"), (removed, "taken out")) if handles)


def _and(names: Any) -> str:
    names = list(names)
    return " and ".join(names) if len(names) < 3 else ", ".join(names[:-1]) + f" and {names[-1]}"


def _link_time(at: int) -> str:
    return datetime.fromtimestamp(at).strftime("%a %d %b %H:%M")


def _print_room_choices(label: str, choices: Any, indent: str = "") -> None:
    tools = _ROOM_TOOLS.get(choices.tools, choices.tools)
    if choices.custom_tools and choices.tools in ("data", "custom"):
        tools += f": {', '.join(choices.custom_tools)}"
    print(f"{indent}{_printable(label)}:")
    print(f"{indent}  Can do: {_printable(tools)}")
    print(f"{indent}  Shares: {_ROOM_SHARE.get(choices.share, choices.share)}")
    print(f"{indent}  Searches your past chats: {'yes' if choices.search_chats else 'no'}")
    print(f"{indent}  Takes part: {_ROOM_TAKES_PART.get(choices.takes_part, choices.takes_part)}")
    print(f"{indent}  Turns a day: {choices.daily_limit}")
    if choices.note:
        print(f"{indent}  Note: {_printable(choices.note.replace(chr(10), ' / '))}")


def _pulse_report(pulse_store: Any, value: str) -> int:
    from . import pulse_report
    if value in ("", "now"):
        if not pulse_store.get("enabled"):
            print("Pulse is off: `hermes conduit-push pulse on` first.")
            return 1
        pulse_store.set("report_requested", True)
        print("Asked the dashboard for a report; it goes into the home chat within a minute or two.")
        return 0
    if value in ("on", "off"):
        pulse_store.set("daily_report", value == "on")
        print("The report is on." if value == "on" else "The report is off.")
        return 0
    if value in ("all-profiles", "this-profile"):
        pulse_store.set("report_profiles", {"all-profiles": "all", "this-profile": "this"}[value])
        print("The report and memory read every profile's chats, memories and cron jobs."
              if value == "all-profiles" else "The report and memory read only this profile.")
        return 0
    times = pulse_report.parse_times(value)
    if times:
        pulse_store.set("report_time", times)
        print(f"The report goes out at {pulse_report.clock_text(times)}.")
        return 0
    if pulse_report.known_zone(value):
        pulse_store.set("time_zone", value)
        print(f"Report times are in {value}.")
        return 0
    print("Usage: hermes conduit-push pulse report [now|on|off|HH:MM[,HH:MM...]|<time zone>|all-profiles|this-profile]")
    return 2


MEMORY_LIST = 50


NUDGE_TEXT = {
    "gentle": "only what's worth interrupting you for, 1 reminder a day, checks spaced out",
    "balanced": "a heads-up on what's open or coming due, 3 reminders a day",
    "eager": "small things too, 6 reminders a day, checks come round sooner",
}


def _pulse_nudge(pulse_store: Any, value: str) -> int:
    from . import pulse_control, pulse_learning
    if value:
        try:
            pulse_control.update(pulse_store, {"nudge": value})
        except pulse_control.Refused:
            print("Usage: hermes conduit-push pulse nudge [gentle|balanced|eager]")
            return 2
    level = pulse_learning.nudge(pulse_store)
    print(f"Pulse nudges: {level} ({NUDGE_TEXT[level]}).")
    if value:
        print("Checks already booked keep their time and take the new pace from their next check.")
    return 0


def _quiet_lines(pulse_store: Any, zone: Any, now: float) -> list[str]:
    """The quiet times (pulse_quiet), for `pulse quiet` and `pulse status`:
    the days, the spell, and whether it's quiet now."""
    from . import pulse_quiet
    days = pulse_quiet.days(pulse_store)
    lines = ["Quiet days: " + (", ".join(pulse_quiet.DAYS[day].capitalize() for day in days) if days else "none")]
    end = pulse_quiet.until(pulse_store)
    if end is not None and end > now:
        lines.append("Quiet until " + datetime.fromtimestamp(end, zone).strftime("%a %d %b %H:%M"))
    if pulse_quiet.why(pulse_store, zone, now):
        ends = pulse_quiet.ends_at(pulse_store, zone, now)
        lines.append("Pulse is quiet now: nothing on its own "
                     + (f"until {datetime.fromtimestamp(ends, zone).strftime('%a %d %b %H:%M')}" if ends
                        else "(every day is a quiet day)") + ".")
    return lines


def _pulse_quiet(pulse_store: Any, home: Any, value: str, extra: str) -> int:
    """Shows or sets the quiet times (pulse_quiet): `quiet today`, `quiet
    until 2026-11-03`, `quiet off`, `quiet days sat,sun` or `quiet days none`."""
    from . import pulse, pulse_control, pulse_quiet
    usage = "Usage: hermes conduit-push pulse quiet [today | until YYYY-MM-DD | off | days sat,sun | days none]"
    try:
        if value == "today":
            pulse_control.update(pulse_store, {"quiet_until": "today"}, home)
        elif value == "until":
            pulse_control.update(pulse_store, {"quiet_until": extra}, home)
        elif value == "off":
            pulse_control.update(pulse_store, {"quiet_until": None}, home)
        elif value == "days":
            names = [] if extra == "none" else [name.strip().lower() for name in extra.split(",") if name.strip()]
            if not names and extra != "none" or any(name not in pulse_quiet.DAYS for name in names):
                print(usage)
                return 2
            pulse_control.update(pulse_store, {"quiet_days": [pulse_quiet.DAYS.index(name) for name in names]}, home)
        elif value:
            print(usage)
            return 2
    except pulse_control.Refused as error:
        print("Quiet days are weekdays: mon to sun." if error.data.get("field") == "quiet_days"
              else "Quiet until a day means a day ahead, within a year: YYYY-MM-DD.")
        return 2
    for line in _quiet_lines(pulse_store, pulse.report_zone(pulse_store, home), time.time()):
        print(line)
    return 0


def _night_line(pulse_store: Any, zone: Any, now: float) -> str:
    """The night (pulse_hours), for `pulse night` and `pulse status`."""
    from . import pulse_hours
    night = pulse_hours.night_text(pulse_store)
    if not night:
        return "Night: none (Pulse may push at any hour)"
    return (f"Night: {night[0]} to {night[1]} ({zone}): nothing pushes then; what would waits for the morning "
            "brief, unless it can't wait" + (". It's night now." if pulse_hours.is_night(pulse_store, zone, now)
                                             else "."))


def _pulse_night(pulse_store: Any, home: Any, value: str, extra: str) -> int:
    """Shows or sets the night (pulse_hours): `night 22:00 07:00`, or `night off`."""
    from . import pulse, pulse_control
    if value:
        try:
            pulse_control.update(pulse_store, {"night": [] if value == "off" and not extra else [value, extra]}, home)
        except pulse_control.Refused:
            print("Usage: hermes conduit-push pulse night [HH:MM HH:MM | off]: when the night starts and ends, "
                  "like 22:00 07:00")
            return 2
    print(_night_line(pulse_store, pulse.report_zone(pulse_store, home), time.time()))
    return 0


def _pulse_note(pulse_store: Any, value: str) -> int:
    """Shows, sets or clears the user's note to Pulse, which every report,
    brief and check reads (pulse_report.note)."""
    from . import pulse_control, pulse_report
    if value:
        try:
            pulse_control.update(pulse_store, {"note": "" if value == "clear" else value})
        except pulse_control.Refused:
            print(f"The note can be at most {pulse_control.NOTE_LIMIT} characters.")
            return 2
    note = " ".join(pulse_report.note(pulse_store).split())
    if note:
        print(f"Note to Pulse: {_printable(note)}")
        print("Its reports, morning brief and reminders read it before they pick what to say.")
    else:
        print("No note to Pulse. `hermes conduit-push pulse note \"what you want from it\"` sets one, in your own "
              "words: what to always bring up, what to leave out.")
    return 0


def _pulse_budget(pulse_store: Any, which: str, value: str) -> int:
    from . import pulse, pulse_control
    field = {"memory": "memory_daily_tokens", "followups": "followup_daily_tokens"}.get(which)
    if field is None:
        print("Usage: hermes conduit-push pulse budget memory|followups [tokens a day|auto]")
        return 2
    if value:
        try:
            amount = None if value == "auto" and field == "followup_daily_tokens" else int(value.replace(",", "").replace("_", ""))
            pulse_control.update(pulse_store, {field: amount})
        except (ValueError, pulse_control.Refused):
            print(f"Give a whole number of tokens a day from {pulse_control.MIN_DAILY_TOKENS:,} to "
                  f"{pulse_control.MAX_DAILY_TOKENS:,}" + (", or 'auto'." if field == "followup_daily_tokens" else "."))
            return 2
    _, cap = pulse._spent_today(pulse_store, "memory" if which == "memory" else "followup", "")
    auto = field == "followup_daily_tokens" and not isinstance(pulse_store.get(field), int)
    print(f"{'Memory' if which == 'memory' else 'Follow-ups'} can use {cap:,} tokens a day"
          + (" (the nudge level's)." if auto else "."))
    return 0


def _pulse_followups(pulse_store: Any, home: Any, value: str) -> int:
    from . import pulse, pulse_followup
    if value in ("on", "off"):
        pulse_store.set("followups", value == "on")
        if value == "off":
            pulse_store.set(pulse_followup.NOW_REQUESTED, None)  # a `now` not yet taken doesn't wait for "on"
        print("Pulse follow-ups are off: what Hermes promised and what you said you'd do are no longer checked on."
              if value == "off" else
              "Pulse follow-ups are on: Pulse checks on what Hermes promised and reminds you of what you said "
              "you'd do, in its chat."
              + ("" if pulse_store.get("memory") else " They need memory too: `hermes conduit-push pulse memory on`."))
        return 0
    if value == "now":
        from . import pulse_learning
        off = [name for name, on in (("Pulse", pulse_store.get("enabled")), ("memory", pulse_store.get("memory")),
                                     ("follow-ups", pulse_store.get("followups")),
                                     ("follow-ups' loudness", pulse_learning.level(pulse_store, pulse_followup.FOLLOWUP)
                                      != "off")) if not on]
        if off:
            print(f"Off: {', '.join(off)}. Turn it on first.")
            return 1
        day = datetime.now(pulse.report_zone(pulse_store, home)).strftime("%Y-%m-%d")
        follow_used, follow_cap = pulse._spent_today(pulse_store, "followup", day)
        if follow_used >= follow_cap:
            print(f"Follow-ups used today's {follow_cap} tokens; checks resume tomorrow.")
            return 1
        pulse_store.set(pulse_followup.NOW_REQUESTED, time.time())
        pulse_store.set("memory_requested", True)
        print("Asked the dashboard to check every promise and to-do now, and to let today's reminders go out again "
              f"(still {pulse_learning.nudging(pulse_store)['reminders']} a day); see `hermes conduit-push pulse followups` in a "
              "minute or two. Meant for testing: each check is a model call.")
        return 0
    if value:
        print("Usage: hermes conduit-push pulse followups [on|off|now]")
        return 2
    rows = pulse_store.followups(MEMORY_LIST)
    if not rows:
        print("Nothing to follow up yet." if pulse_store.get("followups") else "Pulse follow-ups are off.")
        return 0
    zone = pulse.report_zone(pulse_store, home)
    for row in rows:
        print(_printable(pulse_followup.describe(row, pulse_store.item(row["item_id"]), zone)))
    return 0


def _learning_status(pulse_store: Any) -> str:
    from . import pulse_learning
    moved = [f"{pulse_learning.KIND_NAMES.get(kind, kind)} {pulse_learning.LEVEL_NAMES.get(level, level)}"
             for kind, level in pulse_learning.levels(pulse_store).items()
             if level != pulse_learning.DEFAULT_LEVELS[kind]]
    lessons = len(pulse_learning.learned(pulse_store))
    unseen = pulse_store.unseen_cards()
    parts = moved + ([f"{lessons} thing(s) learned from ratings"] if lessons else []) \
        + ([f"{unseen} unseen card(s)"] if unseen else [])
    return ("Pulse screen: " + "; ".join(parts)) if parts else ""


def _pulse_learning(pulse_store: Any, action: str, value: str, extra: str, home: Any = None) -> int:
    from . import pulse, pulse_control, pulse_followup, pulse_learning, pulse_store as store
    if action == "cards":
        zone = pulse.report_zone(pulse_store, Path(home) if home else Path("."))  # the user's, as `pulse status`
        cards = pulse_store.cards(MEMORY_LIST)
        if not cards:
            print("No cards yet: Pulse hasn't brought you anything.")
        marks = pulse_store.done_marks([card["id"] for card in cards])
        for card in cards:
            rated = f" (rated {card['rating']}{', ' + card['reason'] if card['reason'] else ''})" if card["rating"] else ""
            # Pulse marks one done when its chat, or another, shows it done.
            finished = ("" if card["id"] not in marks else " (done, seen in a chat)"
                        if marks[card["id"]]["done_by"] == store.PULSE else " (done)")
            if card.get("dismissed_at"):
                back = card.get("rest_until")
                finished += f" (put away, back {datetime.fromtimestamp(back, zone).strftime('%a %H:%M')})" if back \
                    else " (put away)"
            # Back after a Do it that stalled (pulse_followup.come_back).
            finished += {pulse_followup.UNSENT: " (back: Do it left unsent)",
                         pulse_followup.NOT_DONE: " (back: not done after Do it)"}.get(card.get("back") or "", "")
            topic = f" [{card['topic']}]" if card["topic"] else ""
            print(_printable(f"#{card['id']} [{card['level']}]{topic} {card['title']}: {card['news']}{rated}{finished}"))
        return 0
    try:
        if action == "rate":
            if not value.isdecimal() or len(value) > 18 or not extra:
                print("Usage: hermes conduit-push pulse rate <card> "
                      "up|down|not_relevant|already_knew|too_late|too_often|wrong_time")
                return 2
            rating, reason = ("down", extra) if extra in pulse_learning.REASONS else (extra, "")
            made = pulse_control.rate(pulse_store, int(value), rating, reason, home=home)
            card = pulse_store.card(int(value)) if reason == pulse_learning.WRONG_TIME else None
            back = card.get("rest_until") if card else None
            zone = pulse.report_zone(pulse_store, Path(home) if home else Path("."))
            print("Rated." + (f" Put away until {datetime.fromtimestamp(back, zone).strftime('%a %H:%M')}."
                              if back else "")
                  + "".join(f"\nPulse learned: {_printable(lesson['text'])}" for lesson in made))
            return 0
        if action == "done":
            if not value.isdecimal() or len(value) > 18 or extra not in ("", "undo"):
                print("Usage: hermes conduit-push pulse done <card> [undo]")
                return 2
            changed = pulse_control.done(pulse_store, int(value), extra != "undo")
            print(("Marked done: Pulse counts it as handled." if extra != "undo" else "Back as it was.") if changed
                  else "Nothing changed.")
            return 0
        if value == "undo":
            if not extra.isdecimal() or len(extra) > 18:
                print("Usage: hermes conduit-push pulse learned undo <lesson>")
                return 2
            print("Undone." if pulse_control.undo(pulse_store, int(extra)) else "Nothing to undo with that number.")
            return 0
        if value == "reset":
            found = _resettable(pulse_control.learned(pulse_store, home)["preferences"])
            if extra == "everything":
                scope, about = "everything", ""
            elif extra.isdecimal() and len(extra) <= 3 and 1 <= int(extra) <= len(found):
                scope, about = found[int(extra) - 1]["scope"], found[int(extra) - 1]["value"]
            else:
                print("Usage: hermes conduit-push pulse learned reset <preference number>|everything "
                      "(the numbers `pulse learned` lists)")
                return 2
            lesson = pulse_control.reset(pulse_store, scope, about, home=home)
            print(_printable(f"{lesson['text']}. `hermes conduit-push pulse learned undo {lesson['id']}` takes it back."))
            return 0
        if value:
            if not extra:
                print("Usage: hermes conduit-push pulse learned [undo <lesson> | reset <preference>|everything | "
                      "<kind> push|screen|brief|off]")
                return 2
            lesson = pulse_control.set_level(pulse_store, value, extra)
            print(_printable(lesson["text"]) if lesson else "It's set that way already.")
            return 0
    except pulse_control.Refused as refused:
        if refused.code == "pulse_off":
            print("Pulse is off: `hermes conduit-push pulse on` first.")
            return 1
        print(f"Pulse doesn't take that {refused.data.get('field', 'value')}.")
        return 2
    shown = pulse_control.learned(pulse_store, home)
    for kind, level in shown["levels"].items():
        print(f"{pulse_learning.KIND_NAMES.get(kind, kind)}: {pulse_learning.LEVEL_NAMES.get(level, level)}")
    if not shown["lessons"]:
        print("Nothing learned from ratings yet.")
    for lesson in shown["lessons"]:
        print(_printable(f"#{lesson['id']} {lesson['text']}"))
    if shown["preferences"]:
        print("From your thumbs, Do it and Done (`pulse learned reset <n>` forgets one):")
    numbers = {id(preference): n for n, preference in enumerate(_resettable(shown["preferences"]), 1)}
    for preference in shown["preferences"]:
        # The overall lean counts every vote: only `reset everything` forgets it.
        n = numbers.get(id(preference))
        print(_printable(f"[{n}] {_preference_text(preference)}" if n
                         else f"    {_preference_text(preference)} (`reset everything` forgets it)"))
    return 0


def _resettable(preferences: list) -> list:
    """The preferences `pulse learned reset <n>` takes, in the order listed:
    each but the overall lean (pulse_prefs.RESET_SCOPES)."""
    from . import pulse_prefs
    return [preference for preference in preferences if preference["scope"] in pulse_prefs.RESET_SCOPES]


def _preference_text(preference: dict) -> str:
    """A learned preference (pulse_control.learned) in a line for `pulse learned`."""
    from . import pulse_learning, pulse_prefs
    scope, value = preference["scope"], preference["value"]
    strength = abs(preference["effect"])
    how = "strongly" if strength >= 1.5 else "somewhat" if strength >= 0.5 else "slightly"
    if scope == pulse_prefs.LOUD:
        pushes = preference.get("pushes") or 0
        return (f"{pulse_learning.KIND_NAMES.get(value, value)} come up {how} louder: {pushes} "
                f"push{'' if pushes == 1 else 'es'} acted on within the hour")
    what = (f"things from {preference['chat_title'] or 'a chat'}" if preference.get("chat_title") is not None
            else pulse_learning.preference_words(scope, value))
    lean = "more" if preference["effect"] > 0 else "less"
    votes = preference.get("votes") or 0
    counted = (f": useful {pulse_prefs._number(preference.get('useful') or 0)} of {pulse_prefs._number(votes)} times"
               if votes else "")
    return f"{how} {lean} of {what}{counted}"


CANDIDATE_RUNS = 50
RUN_STATES = {"working": "being made", "ready": "ready to send", "failed": "failed", "sent": "sent",
              "dropped": "too late to send"}


def _pulse_candidates(pulse_store: Any, home: Any, value: str, extra: str = "") -> int:
    """What the latest reports' candidate lists put in front of Pulse, and
    what became of each (pulse_candidates); or, with "bring", one a report
    left out brought up on the Pulse screen."""
    from . import pulse, pulse_candidates, pulse_control
    if value == "bring":
        if not extra.lstrip("#").isdecimal() or len(extra) > 18:
            print("Usage: hermes conduit-push pulse candidates bring <candidate number>")
            return 2
        try:
            card = pulse_control.bring_up(pulse_store, int(extra.lstrip("#")), home)
        except pulse_control.Refused as refused:
            print("Pulse is off: `hermes conduit-push pulse on` first." if refused.code == "pulse_off"
                  else f"No candidate #{_printable(extra.lstrip('#'))} that a report left out.")
            return 1
        print(_printable(f"On the Pulse screen as card #{card['id']}: {card['news']}"))
        return 0
    if value and (not value.isdecimal() or len(value) > 3):
        print("Usage: hermes conduit-push pulse candidates [how many reports | bring <candidate number>]")
        return 2
    run = pulse_store.get(pulse.REPORT_RUN)
    if isinstance(run, dict) and run.get("state") in RUN_STATES:
        tokens, error = run.get("tokens"), run.get("error")
        state = RUN_STATES[run["state"]]
        if isinstance(run.get("refused"), str) and run["refused"]:
            state = f"sent, but Hermes didn't take it ({run['refused']}); the report is tried again as usual"
        elif isinstance(error, str) and error and run["state"] != "dropped":
            state = f"failed ({error}), so the report {'went' if run['state'] == 'sent' else 'goes'} out as before"
        used = f", {tokens} tokens" if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens else ""
        print(_printable(f"Latest list: {state}{used}"))
    runs = pulse_store.candidate_runs(max(1, min(int(value or 1), CANDIDATE_RUNS)))
    if not runs:
        print("No candidate lists yet. A report makes one when this Hermes gives plugins model access; "
              f"they're kept {pulse_candidates.JOURNAL_DAYS} days.")
        return 0
    zone = pulse.report_zone(pulse_store, home)
    for run_id in runs:
        rows = pulse_store.run_candidates(run_id)
        when = datetime.fromtimestamp(rows[0]["created_at"], zone).strftime("%a %d %b %H:%M")
        picked = sum(row["decision"] == pulse_candidates.SURFACED for row in rows)
        what = "Look ahead" if run_id.startswith(pulse.ahead.RUN_PREFIX) else "Report"
        print(f"{what} of {when}: {picked} picked of {len(rows)}")
        for row in rows:
            print(f"  #{row['id']} " + _printable(pulse_candidates.describe(row)))
    return 0


def _ahead_line(pulse_store: Any, zone: Any) -> str:
    """The look ahead (pulse_ahead), for `pulse ahead` and `pulse status`:
    whether it's on, and when it last ran and what came of it."""
    from . import pulse_ahead
    if not pulse_ahead.enabled(pulse_store):
        return "Look ahead: off (`hermes conduit-push pulse ahead on`)"
    if not pulse_store.get("memory"):
        return "Look ahead: waiting on memory, which it runs with: `hermes conduit-push pulse memory on`"
    last = pulse_store.get(pulse_ahead.LAST)
    if not isinstance(last, dict) or not isinstance(last.get("at"), (int, float)):
        return "Look ahead: on; it hasn't run yet (once a day, an hour before the morning brief)"
    when = datetime.fromtimestamp(last["at"], zone).strftime("%a %d %b %H:%M")
    picked, waits = int(last.get("picked") or 0), int(last.get("moments") or 0)
    came = (f"{picked} brought up" + (", held for the morning brief" if last.get("held") else "")
            + (f", {waits} held for a place" if waits else "")) if picked else "nothing brought up"
    error = last.get("error")
    why = (" (the follow-ups' budget was spent, so dates came up in Pulse's own words)" if error == "budget"
           else f" (its model call failed: {error}; dates came up in Pulse's own words)" if error else "")
    return f"Look ahead: on; last ran {when}: {came}{why}"


def _pulse_dates(pulse_store: Any, home: Any) -> int:
    """The dates that come back every year that Pulse knows (pulse_dates),
    and when each next comes up."""
    from . import pulse, pulse_ahead, pulse_dates
    zone = pulse.report_zone(pulse_store, home)
    today = pulse_dates.local_today(zone, time.time())
    found = pulse_dates.yearly(pulse_store, Path(home),
                               None if pulse_store.get("report_profiles") == "all" else "")
    if not found:
        print("Pulse knows no dates that come back every year yet: it learns birthdays and anniversaries from "
              "what's said in chats, and the birthdays of the people you pick in Conduit's Settings > Calendar & "
              "phone > People.")
        return 0
    decided = pulse_store.journaled_refs([ref for entry in found for ref in pulse_dates.refs_round(entry, today)])
    print("Dates that come back every year:")
    for entry in sorted(found, key=lambda entry: pulse_dates.next_on(entry.month, entry.day, today)):
        on = pulse_dates.next_on(entry.month, entry.day, today)
        [coming] = pulse_dates.coming([entry], today, days=366)
        if entry.item is None:
            what = entry.what + (f" ({entry.relation}, picked in Conduit)" if entry.relation else " (picked in Conduit)")
        else:
            what = f"{entry.what} (remembered #{entry.item['id']})"
        up, stage = pulse_dates.next_due(entry, today, decided)
        print(_printable(f"- {what}: {pulse_dates.day_words(on)} {on.year}{pulse_dates.years_words(coming)}; "
                         f"comes up {'today' if up == today else pulse_dates.day_words(up)}, "
                         f"{pulse_dates.STAGE_WORDS[stage]}"))
    if not pulse_ahead.enabled(pulse_store) or not pulse_store.get("memory"):
        print("They come up with the look ahead. " + _ahead_line(pulse_store, zone))
    return 0


def _pulse_ahead(pulse_store: Any, home: Any, value: str) -> int:
    """Shows the latest look ahead (pulse_ahead) and what it weighed; `on`
    and `off` switch it, `now` asks for one at the next memory run."""
    from . import pulse, pulse_ahead, pulse_candidates
    if value in ("on", "off"):
        pulse_store.set(pulse_ahead.SETTING, value == "on")
        if value == "off":
            pulse_store.set(pulse_ahead.REQUESTED, False)  # a `now` not yet taken doesn't wait for "on"
        print("The look ahead is off: Pulse no longer looks two weeks ahead, and birthdays and anniversaries no "
              "longer come up on their own." if value == "off" else
              "The look ahead is on: once a day, an hour before the morning brief, Pulse looks two weeks ahead "
              "for dates and plans worth getting ready for."
              + ("" if pulse_store.get("memory") else " It runs with memory: `hermes conduit-push pulse memory on`."))
        return 0
    if value == "now":
        off = [name for name, on in (("Pulse", pulse_store.get("enabled")), ("memory", pulse_store.get("memory")),
                                     ("the look ahead", pulse_ahead.enabled(pulse_store))) if not on]
        if off:
            print(f"Off: {', '.join(off)}. Turn it on first.")
            return 1
        pulse_store.set(pulse_ahead.REQUESTED, True)
        pulse_store.set("memory_requested", True)
        print("Asked the dashboard for a look ahead at the next memory run; see `hermes conduit-push pulse ahead` "
              "in a minute or two. It's a model call, and what it brings up is held for the brief while today's is "
              "still to come.")
        return 0
    if value:
        print("Usage: hermes conduit-push pulse ahead [now|on|off]")
        return 2
    zone = pulse.report_zone(pulse_store, home)
    print(_ahead_line(pulse_store, zone))
    last = pulse_store.get(pulse_ahead.LAST)
    rows = pulse_store.run_candidates(last["run"]) if isinstance(last, dict) and isinstance(last.get("run"), str) \
        and last["run"] else []
    for row in rows:
        print(f"  #{row['id']} " + _printable(pulse_candidates.describe(row)))
    return 0


def _inbox_line(pulse_store: Any, zone: Any) -> str:
    """Deadlines from the Shortcuts inbox (pulse_memory.read_inbox), for
    `pulse inbox` and `pulse status`: whether memory reads it, how many it
    kept, and its last read."""
    from . import pulse_memory
    title = "Deadlines from the Shortcuts inbox"
    if not pulse_memory.inbox_on(pulse_store):
        return f"{title}: off (`hermes conduit-push pulse inbox on`)"
    if not pulse_store.get("memory"):
        return f"{title}: waiting on memory, which reads them: `hermes conduit-push pulse memory on`"
    kept = len(pulse_store.deadlines())
    last = pulse_store.get(pulse_memory.INBOX_LAST)
    if not isinstance(last, dict) or not isinstance(last.get("at"), (int, float)):
        return f"{title}: on; {kept} kept; nothing read yet"
    when = datetime.fromtimestamp(last["at"], zone).strftime("%a %d %b %H:%M")
    found = int(last.get("deadlines") or 0)
    skipped = int(last.get("skipped") or 0)
    skipped = f"; {skipped} skipped after failed reads" if skipped else ""
    error = f"; it failed: {_printable(str(last['error']))}" if last.get("error") else ""
    return (f"{title}: on; {kept} kept; last read {when}: {int(last.get('events') or 0)} events, "
            f"{found} new deadline{'' if found == 1 else 's'}{skipped}{error}")


def _pulse_inbox(pulse_store: Any, home: Any, value: str) -> int:
    """Whether Pulse memory keeps deadlines from the Shortcuts inbox
    (pulse_memory.read_inbox), and the ones it kept, each with when it next
    comes up; `on` and `off` switch it, and off forgets them."""
    from . import pulse, pulse_dates, pulse_memory
    if value in ("on", "off"):
        pulse_store.set(pulse_memory.store.INBOX_SETTING, value == "on")
        if value == "off":
            gone = pulse_memory.stop_inbox(pulse_store)
            print("Pulse memory no longer reads the Shortcuts inbox for deadlines"
                  + (f", and forgot the {gone} it kept." if gone else "."))
        else:
            print("Pulse memory reads the Shortcuts inbox for deadlines, like a trial ending or a bill due, and "
                  "brings each up a week ahead and the day before, held for the morning brief."
                  + ("" if pulse_store.get("memory") else " It reads with memory: `hermes conduit-push pulse memory on`."))
        return 0
    if value:
        print("Usage: hermes conduit-push pulse inbox [on|off]")
        return 2
    zone = pulse.report_zone(pulse_store, home)
    print(_inbox_line(pulse_store, zone))
    mark = pulse_memory._mark(pulse_store.get(pulse_memory.store.INBOX_MARK))
    if mark:
        print(f"Read what the phone logged up to {datetime.fromtimestamp(mark[0], zone):%a %d %b %H:%M}.")
    today = pulse_dates.local_today(zone, time.time())
    found = pulse_dates.deadlines(pulse_store)
    decided = pulse_store.journaled_refs([ref for entry in found for ref in pulse_dates.refs_round(entry, today)])
    for entry in found:
        up = pulse_dates.next_due(entry, today, decided)
        when = ("brought up already" if up is None
                else f"comes up {'today' if up[0] == today else pulse_dates.day_words(up[0])}, "
                     f"{pulse_dates.STAGE_WORDS[up[1]]}")
        sender = f", from {entry.item['sender']}" if entry.item.get("sender") else ""
        print(_printable(f"- #{entry.item['id']} {entry.what}: {pulse_dates.day_words(entry.on)} {entry.on.year}"
                         f"{sender}; {when}"))
    return 0


def _look_line(pulse_store: Any) -> str:
    """Whether Pulse's turns look things up first (pulse_tools), for `pulse
    look` and `pulse status`, with today's look-ups against their caps."""
    from . import pulse, pulse_tools
    title = "Looking things up first"
    if not pulse.looks_first(pulse_store):
        return f"{title}: off (`hermes conduit-push pulse look on`)"
    today = pulse.looks_today(pulse_store)
    parts = []
    for label in pulse_tools.LABELS:
        used, cap = today.get(label, 0), pulse_tools.DAILY.get(label)
        if used or cap:
            parts.append(f"{label} {used}" + (f" of {cap}" if cap else ""))
    return f"{title}: on; today (UTC) {', '.join(parts)}"


def _pulse_look(pulse_store: Any, home: Any, value: str) -> int:
    """Whether Pulse's report, follow-up and brief turns may look things up
    first (pulse_tools), today's look-ups, what recent turns looked up, and
    what they suggested adding (pulse_proposals); `on` and `off` switch it."""
    from . import pulse, pulse_proposals
    if value in ("on", "off"):
        pulse_store.set(pulse.store.LOOK_SETTING, value == "on")
        print("Pulse's turns look things up before they speak: the calendar, reminders, alarms, contacts, and Maps "
              "between places they name, never where you are; on a follow-up about what was said in a chat, the "
              "web too. Nothing they call can change anything: an add to the calendar or reminders becomes a "
              "suggestion you add with one tap." if value == "on"
              else "Pulse's turns no longer look anything up or suggest adds: they use no tools.")
        return 0
    if value:
        print("Usage: hermes conduit-push pulse look [on|off]")
        return 2
    print(_look_line(pulse_store))
    zone = pulse.report_zone(pulse_store, home)
    for row in pulse_store.looked_attempts(10):
        when = datetime.fromtimestamp(row["created_at"], zone).strftime("%a %d %b %H:%M")
        print(f"- {when} {row['kind']}: {', '.join(pulse.store.looked_labels(row['looked']))}")
    now = time.time()
    suggested = pulse_store.proposals(limit=10)
    if suggested:
        # At most DAILY a day, so the ten newest hold all of today's.
        today = sum(1 for proposal in suggested if proposal["created_at"] >= pulse_proposals.day_from(now))
        print(f"Suggestions: {today} of {pulse_proposals.DAILY} today (UTC)")
        for proposal in suggested:
            when = datetime.fromtimestamp(proposal["created_at"], zone).strftime("%a %d %b %H:%M")
            state = pulse_proposals.entry(proposal, Path(home), now)["state"].replace("_", " ")
            print(_printable(f"- {when} {proposal['line']}: {state}"))
    return 0


def _where_line(pulse_store: Any, home: Any) -> str:
    from . import pulse_places
    title = "Where you are"
    if not pulse_places.on(pulse_store):
        return f"{title}: off (`hermes conduit-push pulse where on`)"
    if not pulse_places.background(pulse_places.view(Path(home)), time.time()):
        return (f"{title}: on, but Conduit isn't sharing your location in the background, so Pulse reads none "
                "(Settings > Location in Conduit)")
    return f"{title}: on"


def _pulse_where(pulse_store: Any, home: Any, value: str) -> int:
    """Whether Pulse reads where the user is (pulse_places), what waits for
    a place (what memory keeps, and the look ahead's picks), and what the
    last day's arrivals and departures did; `on` and `off` switch it."""
    from . import pulse, pulse_followup, pulse_places
    if value in ("on", "off"):
        pulse_store.set(pulse.store.WHERE_SETTING, value == "on")
        if value == "off":
            pulse_store.clear_moments()
            print("Pulse no longer reads where you are. What it remembers for a place stays, and waits for you to "
                  "turn this back on; what the look ahead held for a place comes to the Pulse screen without a "
                  "notification" + ("." if pulse_store.get("enabled") else " once Pulse is on again."))
        else:
            print("Pulse reads where you are while Conduit shares it in the background: a reminder you asked for "
                  "at a place comes when you get there, and its look-ups may use your location. Each read goes in "
                  "Location's read log in Conduit.")
        return 0
    if value:
        print("Usage: hermes conduit-push pulse where [on|off]")
        return 2
    print(_where_line(pulse_store, home))
    zone = pulse.report_zone(pulse_store, home)
    saved = pulse_places.saved(pulse_places.view(Path(home)))
    waiting = pulse_places.waiting(pulse_store)
    if waiting:
        print("Waiting for a place:")
        for item in waiting:
            match = pulse_places.place_for(item["place_words"], saved)
            leaving = "leaving " if item["place_on"] == "leave" else ""
            row = pulse_store.followup(item["id"])
            until = ""
            if row and row["decision"] == pulse_followup.PLACE and row["next_check_at"]:
                until = (", checking now" if pulse_followup.followup_timing(row).get("moment") else
                         f", until {datetime.fromtimestamp(row['next_check_at'], zone).strftime('%a %d %b')}")
            print(_printable(f"- #{item['id']} {item['text']} ({leaving}{item['place_words']}: "
                             + (f"matches {match}" if match else "no saved place has that name") + f"{until})"))
    else:
        print("Nothing waits for a place.")
    held = pulse_store.moment_cards()
    if held:
        print("Held by the look ahead for a place:")
        for card in held:
            print(_printable(pulse_places.held_line(card, zone)))
    moments = pulse_store.moments(since=time.time() - pulse.store.MOMENTS_KEEP_S)
    if moments:
        texts = {item["id"]: item["text"] for item in waiting}
        picks = {card["id"]: card for card in held}
        for moment in moments:
            for item_id in _moment_items(moment):
                if item_id not in texts:
                    found = pulse_store.item(item_id)
                    texts[item_id] = found["text"] if found else f"#{item_id}"
            for card_id in _moment_picks(moment):
                if card_id not in picks:
                    picks[card_id] = pulse_store.card(card_id)
        print("Arrivals and departures in the last day:")
        for moment in moments:
            print(_printable(pulse_places.describe(moment, zone, texts, picks)))
    return 0


def _private_line(pulse_store: Any) -> str:
    """Whether a private Pulse push hides its words (pulse_push), for `pulse
    private` and `pulse status`."""
    from . import pulse_push
    if pulse_push.hides_private(pulse_store):
        return (f'Private reminders: the Lock Screen says only "{pulse_push.PRIVATE_BODY}" '
                "(`hermes conduit-push pulse private off` to show their words)")
    return "Private reminders: shown like any other push (`hermes conduit-push pulse private on`)"


def _time_sensitive_line(pulse_store: Any, zone: Any) -> str:
    """Today's time-sensitive Pulse pushes against their cap (pulse_push), for `pulse status`."""
    from . import pulse_push
    sent = pulse_push.sent_today(pulse_store.get(pulse_push.TIME_SENSITIVE_KEY), datetime.now(zone).date().isoformat())
    return f"Time-sensitive pushes today: {min(sent, pulse_push.TIME_SENSITIVE_DAILY)} of {pulse_push.TIME_SENSITIVE_DAILY}"


def _pulse_private(pulse_store: Any, value: str) -> int:
    """Whether a Pulse push about a gift, a surprise, someone's day, health
    or money hides its words on the Lock Screen (pulse_push); `on` and `off`
    switch it."""
    from . import pulse_push
    if value in ("on", "off"):
        pulse_store.set(pulse_push.PRIVATE_SETTING, value == "on")
        print("A Pulse push about a gift, a surprise, someone's birthday or anniversary, health or money says only "
              f'"{pulse_push.PRIVATE_BODY}", even with Show previews on; the Pulse screen shows all of it.'
              if value == "on" else "Pulse pushes show their words like any other, as Show previews says.")
        return 0
    if value:
        print("Usage: hermes conduit-push pulse private [on|off]")
        return 2
    print(_private_line(pulse_store))
    return 0


def _moment_outcome(moment: dict) -> dict:
    try:
        outcome = json.loads(moment.get("outcome") or "{}")
    except (TypeError, ValueError):
        return {}
    return outcome if isinstance(outcome, dict) else {}


def _moment_items(moment: dict) -> list[int]:
    from .pulse_places import ids_in
    outcome = _moment_outcome(moment)
    return [*(ids_in(outcome.get("due")) or ids_in(outcome.get("items"))), *ids_in(outcome.get("related"))]


def _moment_picks(moment: dict) -> list[int]:
    """The held picks a moment brought up, or left waiting (pulse_places.bring_up)."""
    from .pulse_places import ids_in
    outcome = _moment_outcome(moment)
    return [*ids_in([outcome.get("pick")]), *ids_in(outcome.get("picks"))]


def _pulse_memory(pulse_store: Any, home: Any, value: str, extra: str) -> int:
    from . import pulse, pulse_memory
    if value == "now":
        if not pulse_store.get("enabled"):
            print("Pulse is off: `hermes conduit-push pulse on` first.")
            return 1
        if not pulse_store.get("memory"):
            print("Pulse memory is off: `hermes conduit-push pulse memory on` first.")
            return 1
        pulse_store.set("memory_requested", True)
        print("Asked the dashboard to read quiet chats now; see `hermes conduit-push pulse memory` in a minute or two.")
        return 0
    if value in ("on", "off"):
        pulse_store.set("memory", value == "on")
        if value == "off":
            print("Pulse memory is off. What it kept stays until you forget it.")
        elif pulse_store.get("enabled"):
            print("Pulse memory is on: chats are read once they've been quiet for 20 minutes.")
        else:
            print("Pulse memory is on, and reads chats once Pulse is on: `hermes conduit-push pulse on`.")
        return 0
    if value == "forget":
        if not extra.lstrip("#").isdecimal() or len(extra) > 18:
            print("Usage: hermes conduit-push pulse memory forget <item number>")
            return 2
        if not pulse_store.close_item(int(extra.lstrip("#")), status="cancelled", closed_by="user"):
            print(f"No open item #{_printable(extra.lstrip('#'))}.")
            return 1
        print(f"Forgot #{extra.lstrip('#')}.")
        return 0
    if value == "backfill":
        return _pulse_backfill(pulse_store, home, extra)
    if value and value not in pulse_memory.ALL_KINDS:
        print("Usage: hermes conduit-push pulse memory [" + "|".join(pulse_memory.ALL_KINDS)
              + "|now|on|off|forget <n>|backfill [days|start|stop|cancel]]")
        return 2
    items = pulse_store.open_items(value, limit=MEMORY_LIST + 1)
    if len(items) > MEMORY_LIST:
        items = items[:MEMORY_LIST]
        print(f"The {MEMORY_LIST} most recent of {sum(pulse_store.item_counts().get(k, 0) for k in ([value] if value else pulse_memory.ALL_KINDS))}:")
    if not items:
        print("Pulse hasn't kept anything yet." if pulse_store.get("memory") else "Pulse memory is off.")
        return 0
    zone = pulse.report_zone(pulse_store, home)
    for kind in pulse_memory.ALL_KINDS:
        group = [item for item in items if item["kind"] == kind]
        if group:
            print(f"{kind.capitalize()} ({len(group)})")
            for item in group:
                for line in pulse_memory.describe(item, zone).split("\n"):
                    print("  " + _printable(line))
    return 0


def _pulse_backfill(pulse_store: Any, home: Any, extra: str) -> int:
    """`pulse memory backfill [days]` shows what reading that far back
    costs; `start` reads the last days shown, `stop` pauses it, `cancel`
    drops an unfinished one."""
    from . import pulse, pulse_control
    zone = pulse.report_zone(pulse_store, home)

    def day(epoch: float) -> str:
        return datetime.fromtimestamp(epoch, zone).strftime("%d %b %Y")

    if extra and extra not in ("start", "stop", "cancel") \
            and not (extra.isdecimal() and len(extra) <= 3 and 1 <= int(extra) <= 365):
        print("Usage: hermes conduit-push pulse memory backfill [days (1-365)|start|stop|cancel]")
        return 2
    try:
        if extra == "stop":
            pulse_control.backfill_stop(pulse_store)
            print("Backfill paused. `hermes conduit-push pulse memory backfill start` carries on where it stopped.")
        elif extra == "cancel":
            pulse_control.backfill_cancel(pulse_store)
            print("Backfill cancelled. What it already read is kept; `hermes conduit-push pulse memory backfill "
                  "<days>` shows a new one.")
        elif extra == "start":
            started = pulse_control.backfill_start(pulse_store, Path(home))
            print(f"Memory is reading chats from {day(started['from'])} to {day(started['to'])}, after new chats "
                  "and within its daily tokens. `hermes conduit-push pulse status` shows when it's done.")
            if not pulse_store.get("enabled"):
                print("It starts once Pulse is on: `hermes conduit-push pulse on`.")
        else:
            cost = pulse_control.backfill_estimate(pulse_store, Path(home), int(extra) if extra else None)
            print(f"Reading back {cost['days']} days ({day(cost['from'])} to {day(cost['to'])}) covers "
                  f"{cost['chats']} chats, about {cost['chars']:,} characters in {cost['chunks']} chunks: roughly "
                  f"{cost['tokens']:,} tokens on Hermes's \"Pulse memory\" model, about {cost['days_needed']} day(s) "
                  f"at its {cost['daily_tokens']:,} tokens a day (shared with reading new chats).")
            if cost["unreadable"]:
                print("Couldn't read profile(s) " + ", ".join(_printable(name) for name in cost["unreadable"])
                      + " just now; they aren't counted, and the backfill won't finish until they can be read.")
            if cost["chats"]:
                print("To read them: `hermes conduit-push pulse memory backfill start`.")
        return 0
    except pulse_control.Refused as refused:
        data = refused.data
        if refused.code == "reading":
            print(f"Memory is already reading chats back to {day(data['from'])}. "
                  "`hermes conduit-push pulse memory backfill stop` pauses it.")
            return 1 if extra == "start" else 0
        if refused.code == "paused":
            print(f"A paused backfill back to {day(data['from'])} carries on first: "
                  "`hermes conduit-push pulse memory backfill start`, or `... backfill cancel` drops it. "
                  "Once it's done, a backfill can go further back.")
            return 0
        if refused.code == "already_read":
            print(f"Memory has already read back {data['days']} days or more.")
            return 0
        print({"not_running": "No backfill is running.",
               "nothing_to_cancel": "No backfill to cancel.",
               "changed": "The backfill changed meanwhile; try again.",
               "memory_off": "Pulse memory is off: `hermes conduit-push pulse memory on` first.",
               "no_home_chat": "Pulse needs its home chat first, so its own reports aren't read as chats. "
                               + _printable(str(data.get("problem", ""))),
               "unreadable": f"Couldn't read Hermes's chats ({data.get('error')})."}.get(refused.code, refused.code)
              .strip())
        return 1


def _pulse_claims(pulse_store: Any, home: Any, value: str) -> int:
    """What Hermes said in a chat it had just done with a tool, which memory
    kept to judge (pulse_claims): the ones no call backed, or with `all`
    every one, with the call that backed it. Nothing shows in Conduit yet."""
    from . import pulse, pulse_claims, pulse_hermes
    if value not in ("", "all"):
        print("Usage: hermes conduit-push pulse claims [all]")
        return 2
    kept = pulse_store.claims(unbacked=value != "all")
    since = pulse_claims.judged_from(pulse_store)
    if not kept:
        print(("No claims kept yet" if value == "all" else "No claims without a call behind them")
              + (". Memory notes them in chats it reads once they go quiet." if since is not None else
                 ". They're judged while Pulse is on, from the next time its loop runs."))
        return 0
    print("What Hermes said it did" + (", and the call that backed it" if value == "all" else
                                      " with no call behind it in that turn")
          + " (kept to judge; Conduit shows none of it yet):")
    zone = pulse.report_zone(pulse_store, home)
    try:
        titles = pulse_hermes.chat_titles(Path(home), [claim["chat_id"] for claim in kept])
    except pulse_hermes.HermesUnreadable:
        titles = {}
    for claim in kept:
        when = datetime.fromtimestamp(claim["said_at"], zone).strftime("%a %d %b %H:%M")
        chat = titles.get(claim["chat_id"]) or claim["chat_id"]
        print(_printable(f"- #{claim['id']} {when} in \"{chat}\" ({claim['action']}): {claim['text']}"))
        print(_printable(f"  Said: \"{claim['evidence']}\""))
        if claim["backed_by"]:
            print(_printable(f"  Backed by: {claim['backed_by']}"))
        try:
            calls = json.loads(claim["calls_json"])
        except ValueError:
            calls = []
        print(_printable("  That turn's calls: " + ("; ".join(str(call) for call in calls) if calls else "none")))
    return 0


def _pulse_spots(pulse_store: Any, home: Any, value: str) -> int:
    """Walking past (pulse_spots): what the phone watches now, how many it
    said it could, and the shops the user came near with how late iOS told
    the app; `on` and `off` switch it."""
    from . import pulse, pulse_places, pulse_spots
    if value in ("on", "off"):
        pulse_store.set(pulse_spots.SETTING, value == "on")
        if value == "off":
            pulse_store.clear_spot_entries()  # off forgets the shops come near
        print("With Where you are on, the phone watches a few shops near you for what Pulse held for one, and Pulse "
              "brings it up when you come near one." if value == "on" else
              "The phone stops watching shops for Pulse once it next sends its location; what was held for a shop "
              "comes to the Pulse screen at the end of its window.")
        return 0
    if value:
        print("Usage: hermes conduit-push pulse spots [on|off]")
        return 2
    now = time.time()
    zone = pulse.report_zone(pulse_store, home)
    state = "on" if pulse_spots.on(pulse_store) else "off"
    if pulse_spots.on(pulse_store) and not pulse_spots.reads(pulse_store, now, Path(home)):
        state += (" (waiting: Pulse is off)" if not pulse_store.get("enabled") else
                  " (waiting: Where you are is off)" if not pulse_places.on(pulse_store) else
                  " (waiting: the phone isn't sharing its location in the background)")
    elif pulse_spots.on(pulse_store) and not pulse_spots.precise(Path(home)):
        state += " (waiting: the phone shares its location in the background at less than Precise)"
    print(f"Walking past: {state}")
    phone = pulse_store.get(pulse_spots.PHONE)
    if isinstance(phone, dict) and isinstance(phone.get("room"), int):
        said = datetime.fromtimestamp(float(phone.get("at") or 0), zone).strftime("%a %d %b %H:%M")
        print(f"The phone said it could watch {phone['room']} more places ({said}).")
    watching = pulse_spots.watches(pulse_store, Path(home), now)
    print("Watching for:" if watching else "Watching for nothing now.")
    for watch in watching:
        until = datetime.fromtimestamp(watch["until"], zone).strftime("%a %d %b %H:%M")
        circle = watch["radius"]
        size = (f"{circle['walk']:.0f} m" if circle["walk"] == circle["drive"] else
                f"{circle['walk']:.0f} m walking, {circle['drive']:.0f} m driving")
        print(_printable(f"- {watch['query']} ({watch['intent']}, {size}, until {until})"))
    entries = pulse_store.spot_entries()
    print("Came within a watched shop's circle (how late is an estimate from the phone's fix):" if entries
          else "Came within no watched shop's circle yet.")
    for entry in entries:
        print(_printable(pulse_spots.describe(entry, zone)))
    return 0


def _pulse_outcomes(pulse_store: Any, value: str) -> int:
    from . import pulse_ledger
    limit, session_id = 20, None
    if value.isdecimal() and len(value) < 6:
        limit = max(1, min(int(value), 500))
    elif value:
        session_id = value
    rows = pulse_store.recent_outcomes(limit, session_id=session_id)
    if not rows:
        print("No tool outcomes recorded yet." if pulse_store.get("enabled") else
              "No tool outcomes recorded. They're recorded while Pulse is on.")
        return 0
    for row in reversed(rows):
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["at"]))
        print(f"{when}  {_printable(pulse_ledger.line(row))}  [{_printable(row['session_id'])}]")
    return 0


def _flipped(ciphertext: str) -> str:
    raw = bytearray(e2e.unb64u(ciphertext))
    raw[0] ^= 0x01
    return e2e.b64u(bytes(raw))


def _profile_name() -> str:
    try:
        from hermes_cli.profiles import get_active_profile_name
        return get_active_profile_name()
    except Exception:
        return "default"
