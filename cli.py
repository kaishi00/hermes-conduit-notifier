"""`hermes conduit-push` pairing and diagnostics commands."""

from __future__ import annotations

import argparse
import http.client
import socket

from . import e2e
from .client import DEFAULT_RELAY_URL, UnpairedLocally, build_outgoing, claim_pairing, e2e_status, load_state, post_event, send_now, set_redact_content, state_path, unpair
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
    pulse = commands.add_parser("pulse", help="Pulse, the proactivity experiment: on, off, status, probe, home")
    pulse.add_argument("pulse_action", choices=["on", "off", "status", "probe", "home"])
    pulse.add_argument("value", nargs="?", default="",
                       help="for home: the session key of the chat to use, or 'auto' to find it by title")
    parser.set_defaults(func=dispatch)


# What a relay request or a bad saved pairing can raise: printed as one line.
_RELAY_ERRORS = (RuntimeError, OSError, ValueError, LookupError, http.client.HTTPException)


def _printable(value: object) -> str:
    return "".join(c for c in str(value) if c.isprintable())


def dispatch(args: argparse.Namespace) -> int:
    action = getattr(args, "conduit_push_action", None)
    if action == "pair":
        try:
            state = claim_pairing(args.code, args.relay_url, args.name)
        except _RELAY_ERRORS as error:
            print(f"Pairing failed: {error}")
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
                print(f"The Conduit relay did not accept the test notification: {error}")
                return 1
            print("Broken encrypted test notification accepted by the Conduit relay. The phone should show only generic text.")
            return 0
        try:
            send_now(event)
        except _RELAY_ERRORS as error:
            print(f"The Conduit relay did not accept the test notification: {error}")
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
    print("Usage: hermes conduit-push {pair|status|test|redact|unpair|pulse}")
    return 2


def _pulse(args: argparse.Namespace) -> int:
    from hermes_constants import get_hermes_home

    from . import pulse, pulse_delivery
    home = get_hermes_home()
    pulse_store = pulse.open_store(home)
    action = args.pulse_action
    if action in ("on", "off"):
        pulse_store.set("enabled", action == "on")
        if action == "on":
            print("Pulse is on. The gateway sends the first delivery check within a minute; "
                  "see `hermes conduit-push pulse status`.")
        else:
            print("Pulse is off. Nothing more goes into the home chat.")
        return 0
    if action == "probe":
        if not pulse_store.get("enabled"):
            print("Pulse is off: `hermes conduit-push pulse on` first.")
            return 1
        pulse_store.set("probe_requested", True)
        print("Asked the gateway for a delivery check; it goes out within a minute.")
        return 0
    if action == "home":
        value = (args.value or "").strip()
        if not value:
            print("Usage: hermes conduit-push pulse home <session key>|auto")
            return 2
        pulse_store.set("home_session_key", "" if value == "auto" else value)
        print("Pulse finds its chat by title again." if value == "auto" else f"Pulse speaks in {_printable(value)}.")
        return 0
    print(f"Pulse: {'on' if pulse_store.get('enabled') else 'off'}")
    chat, problem = pulse.home_chat(pulse_store, home)
    if chat is not None:
        print(f"Home chat: {_printable(chat.title)} ({_printable(chat.session_key)})"
              f"{'' if chat.pinned else ', not pinned'}")
    if problem:
        print(f"Warning: {_printable(problem)}")
    print(f"Delivery check every {pulse_store.get('probe_interval_hours')} hours")
    counts = pulse_store.counts(pulse.PROBE)
    if counts:
        print("Checks so far: " + ", ".join(f"{n} {state}" for state, n in sorted(counts.items())))
    recent = pulse_store.recent(10)
    if recent:
        print("Latest:")
        for row in recent:
            print("  " + _printable(pulse_delivery.describe(row)))
    print(f"State: {pulse_store.path}")
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
