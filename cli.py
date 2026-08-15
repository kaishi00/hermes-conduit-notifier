"""`hermes conduit-push` pairing and diagnostics commands."""

from __future__ import annotations

import argparse
import socket

from .client import DEFAULT_RELAY_URL, claim_pairing, load_state, send_now, state_path, unpair
from .events import event_id, plugin_hello, push_event


def register_cli(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="conduit_push_action")
    pair = commands.add_parser("pair", help="Pair this Hermes profile with a Conduit device")
    pair.add_argument("code")
    pair.add_argument("--relay-url", default=DEFAULT_RELAY_URL)
    pair.add_argument("--name", default="")
    commands.add_parser("status", help="Show pairing status without revealing credentials")
    commands.add_parser("test", help="Send a test notification to the paired device")
    commands.add_parser("unpair", help="Revoke this profile's relay credential")
    parser.set_defaults(func=dispatch)


def dispatch(args: argparse.Namespace) -> int:
    action = getattr(args, "conduit_push_action", None)
    if action == "pair":
        state = claim_pairing(args.code, args.relay_url, args.name)
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
        print(f"Paired: {state.get('gateway_name') or socket.gethostname()}")
        print(f"Relay: {state.get('relay_url') or DEFAULT_RELAY_URL}")
        print(f"State: {state_path()}")
        return 0
    if action == "test":
        send_now(push_event(
            "response.ready",
            identifier=event_id("test"),
            profile=_profile_name(),
            title="Hermes Conduit",
            body="Push notifications are connected.",
        ))
        print("Test notification accepted by the Conduit relay.")
        return 0
    if action == "unpair":
        print("Conduit pairing revoked." if unpair() else "This Hermes profile was not paired.")
        return 0
    print("Usage: hermes conduit-push {pair|status|test|unpair}")
    return 2


def _profile_name() -> str:
    try:
        from hermes_cli.profiles import get_active_profile_name
        return get_active_profile_name()
    except Exception:
        return "default"
