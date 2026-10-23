"""`hermes conduit-push` pairing and diagnostics commands."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import socket
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import e2e
from .client import DEFAULT_RELAY_URL, RelayRejected, UnpairedLocally, build_outgoing, claim_pairing, e2e_status, load_state, post_event, record_relay_accepted, record_relay_refusal, send_now, set_redact_content, state_path, unpair
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
    pulse = commands.add_parser("pulse", help="Pulse, the proactivity experiment: on, off, status, probe, report, home, outcomes, memory, followups, "
                                                 "cards, rate, learned, nudge, budget")
    pulse.add_argument("pulse_action", choices=["on", "off", "status", "probe", "report", "home", "outcomes", "memory",
                                                "followups", "cards", "rate", "learned", "nudge", "budget"])
    pulse.add_argument("value", nargs="?", default="",
                       help="for home: the id of the chat to use, or 'auto' to find it by title; "
                            "for outcomes: how many to show, or a chat id; "
                            "for report: 'now', 'on', 'off', times like 08:00,13:00, a time zone like Europe/Paris, "
                            "'all-profiles' or 'this-profile'; "
                            "for memory: a kind to list, 'now', 'on', 'off', 'forget' and an item number, "
                            "or 'backfill'; "
                            "for followups: 'on', 'off', 'now' (check everything now, for testing), or nothing to list them; "
                            "for rate: the card number; "
                            "for learned: 'undo' and a lesson number, or a kind and a loudness to set; "
                            "for nudge: gentle, balanced or eager; "
                            "for budget: memory or followups")
    pulse.add_argument("extra", nargs="?", default="",
                       help="for memory forget: the item number; for memory backfill: how many days back "
                            "(it shows what that costs), 'start', 'stop' or 'cancel'; "
                            "for rate: 'up', 'down', or a reason for a thumbs down (not_relevant, already_knew, "
                            "too_late, too_often); for learned: the lesson number, or push, screen, brief or off; "
                            "for budget: tokens a day, or 'auto' for follow-ups to go with the nudge level")
    prefs = commands.add_parser(
        "prefs",
        help="Conduit's synced preferences: export them to a file, or import one from another host",
    )
    prefs.add_argument("prefs_action", choices=["show", "export", "import"])
    prefs.add_argument("file", nargs="?", default="",
                       help="for export: where to write (prints to the terminal without one); for import: the file to read")
    prefs.add_argument("--replace", action="store_true",
                       help="for import: drop the preferences the file doesn't list, instead of keeping them")
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
            print(f"End-to-end encryption: on (key {encryption['kid'][:8]}…)")
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
    print("Usage: hermes conduit-push {pair|status|test|redact|unpair|pulse|prefs}")
    return 2


def _pulse(args: argparse.Namespace) -> int:
    from hermes_constants import get_hermes_home

    from . import pulse, pulse_delivery, pulse_learning, pulse_memory
    home = get_hermes_home()
    action = args.pulse_action
    # Follow-ups are on by default, so even `followups on|off` needs no file yet.
    listing = action in ("followups", "cards", "rate", "learned") \
        or (action == "memory" and (args.value or "").strip() not in ("on", "off")) \
        or (action == "nudge" and not (args.value or "").strip()) \
        or (action == "budget" and not str(getattr(args, "extra", "") or "").strip())
    if (action in ("status", "probe", "off", "outcomes") or listing) and not (Path(home) / pulse_delivery.store.DB_NAME).exists():
        # Never turned on: say so without leaving a file behind.
        print("Pulse: off (never turned on). `hermes conduit-push pulse on` to start it."
              + (" Nothing was changed." if action in ("followups", "learned") and (args.value or "").strip() else ""))
        return 1 if action in ("probe", "rate") or (action == "learned" and (args.value or "").strip()) \
            or (action == "memory" and (args.value or "").strip() in ("now", "forget")) \
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
    if action in ("cards", "rate", "learned"):
        return _pulse_learning(pulse_store, action, (args.value or "").strip(),
                               str(getattr(args, "extra", "") or "").strip())
    if action == "nudge":
        return _pulse_nudge(pulse_store, (args.value or "").strip())
    if action == "budget":
        return _pulse_budget(pulse_store, (args.value or "").strip(), str(getattr(args, "extra", "") or "").strip())
    if action == "memory":
        return _pulse_memory(pulse_store, home, (args.value or "").strip(), str(getattr(args, "extra", "") or "").strip())
    print(f"Pulse: {'on' if pulse_store.get('enabled') else 'off'}")
    chat, problem = pulse.home_chat(pulse_store, home)
    if chat is not None:
        print(f"Home chat: {_printable(chat.title)} ({_printable(chat.chat_id)})"
              f"{'' if chat.pinned else ', not pinned'}")
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
            if isinstance(lease, dict) and lease.get("build") != pulse.build_stamp():
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
    if pulse_store.get("daily_report"):
        zone = pulse.report_zone(pulse_store, home)
        upcoming = pulse.report.next_time(time.time(), zone, pulse_store.get("report_time"))
        print(f"Shadow reports at {pulse.report.clock_text(pulse_store.get('report_time'))} ({zone}); next "
              + datetime.fromtimestamp(upcoming, zone).strftime("%a %d %b %H:%M"))
    else:
        print("Shadow reports: off")
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
    print(f"Nudging: {pulse_learning.nudge(pulse_store)} (`hermes conduit-push pulse nudge gentle|balanced|eager`)")
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


def _pulse_report(pulse_store: Any, value: str) -> int:
    from . import pulse_report
    if value in ("", "now"):
        if not pulse_store.get("enabled"):
            print("Pulse is off: `hermes conduit-push pulse on` first.")
            return 1
        pulse_store.set("report_requested", True)
        print("Asked the dashboard for a shadow report; it goes into the home chat within a minute or two.")
        return 0
    if value in ("on", "off"):
        pulse_store.set("daily_report", value == "on")
        print("The shadow report is on." if value == "on" else "The shadow report is off.")
        return 0
    if value in ("all-profiles", "this-profile"):
        pulse_store.set("report_profiles", {"all-profiles": "all", "this-profile": "this"}[value])
        print("The shadow report and memory read every profile's chats, memories and cron jobs."
              if value == "all-profiles" else "The shadow report and memory read only this profile.")
        return 0
    times = pulse_report.parse_times(value)
    if times:
        pulse_store.set("report_time", times)
        print(f"The shadow report goes out at {pulse_report.clock_text(times)}.")
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


def _pulse_learning(pulse_store: Any, action: str, value: str, extra: str) -> int:
    from . import pulse_control, pulse_learning
    if action == "cards":
        cards = pulse_store.cards(MEMORY_LIST)
        if not cards:
            print("No cards yet: Pulse hasn't brought you anything.")
        for card in cards:
            rated = f" (rated {card['rating']}{', ' + card['reason'] if card['reason'] else ''})" if card["rating"] else ""
            print(_printable(f"#{card['id']} [{card['level']}] {card['title']}: {card['news']}{rated}"))
        return 0
    try:
        if action == "rate":
            if not value.isdecimal() or len(value) > 18 or not extra:
                print("Usage: hermes conduit-push pulse rate <card> up|down|not_relevant|already_knew|too_late|too_often")
                return 2
            rating, reason = ("down", extra) if extra in pulse_learning.REASONS else (extra, "")
            made = pulse_control.rate(pulse_store, int(value), rating, reason)
            print("Rated." + "".join(f"\nPulse learned: {_printable(lesson['text'])}" for lesson in made))
            return 0
        if value == "undo":
            if not extra.isdecimal() or len(extra) > 18:
                print("Usage: hermes conduit-push pulse learned undo <lesson>")
                return 2
            print("Undone." if pulse_control.undo(pulse_store, int(extra)) else "Nothing to undo with that number.")
            return 0
        if value:
            if not extra:
                print("Usage: hermes conduit-push pulse learned [undo <lesson> | <kind> push|screen|brief|off]")
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
    lessons = pulse_learning.learned(pulse_store)
    for kind, level in pulse_learning.levels(pulse_store).items():
        print(f"{pulse_learning.KIND_NAMES.get(kind, kind)}: {pulse_learning.LEVEL_NAMES.get(level, level)}")
    if not lessons:
        print("Nothing learned from ratings yet.")
    for lesson in lessons:
        print(_printable(f"#{lesson['id']} {lesson['text']}"))
    return 0


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
    if value and value not in pulse_memory.KINDS:
        print("Usage: hermes conduit-push pulse memory [" + "|".join(pulse_memory.KINDS)
              + "|now|on|off|forget <n>|backfill [days|start|stop|cancel]]")
        return 2
    items = pulse_store.open_items(value, limit=MEMORY_LIST + 1)
    if len(items) > MEMORY_LIST:
        items = items[:MEMORY_LIST]
        print(f"The {MEMORY_LIST} most recent of {sum(pulse_store.item_counts().get(k, 0) for k in ([value] if value else pulse_memory.KINDS))}:")
    if not items:
        print("Pulse hasn't kept anything yet." if pulse_store.get("memory") else "Pulse memory is off.")
        return 0
    zone = pulse.report_zone(pulse_store, home)
    for kind in pulse_memory.KINDS:
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
