# Hermes Conduit Notifier

Hermes Conduit Notifier is the open-source Hermes plugin that delivers lifecycle notifications to the Hermes Conduit iOS app. It observes normal Hermes hooks and sends small HTTPS events to the Conduit push relay.

The plugin does **not** contain an Apple Push Notification service key, dashboard credentials, or access to your Hermes gateway. Apple credentials remain on the central push relay, so self-hosted users never need to copy a shared signing key onto their gateway.

## Install

Run these commands on the machine where Hermes is installed:

```bash
hermes plugins install kaishi00/hermes-conduit-notifier --enable
hermes gateway restart
```

In Hermes Conduit, open **Settings > Notifications**, enable notifications, and create a pairing code. Claim it from the matching Hermes profile:

```bash
hermes conduit-push pair XXXXX-XXXXX
```

Pair additional profiles independently:

```bash
hermes -p coder plugins enable conduit_push
hermes -p coder conduit-push pair YYYYY-YYYYY
```

Pairing codes expire after ten minutes and can only be claimed once.

## Manage the pairing

```bash
# Show status without printing the credential
hermes conduit-push status

# Send a local test event through the relay
hermes conduit-push test

# Revoke this profile's credential
hermes conduit-push unpair
```

Update the plugin and restart Hermes:

```bash
hermes plugins update conduit_push
hermes gateway restart
```

## Events

The plugin currently emits notifications for:

- approval needed
- clarification or other input needed
- response ready
- failed turns
- completed delegated tasks

The iOS app controls which categories are enabled, whether notification previews are shown, and whether completion sounds play.

## Privacy and security

- Pairing creates a revocable, profile-scoped credential.
- The credential is stored in the profile-aware Hermes home as `conduit-push.json` with mode `0600` on supported systems.
- Authorization credentials are never written to logs or printed by `status`.
- Hook callbacks enqueue bounded events; HTTPS delivery runs on a background worker and does not block the agent loop.
- Event titles and bodies are length-limited before delivery.
- Lock Screen previews are disabled by default in Hermes Conduit.

The public relay URL is part of the client protocol. Relay infrastructure, APNs signing material, and deployment configuration are intentionally not included in this repository.

## Development

The runtime uses only the Python standard library plus Hermes APIs. Run the pure event tests with:

```bash
python -m pytest --rootdir=tests tests
```

## License

MIT
