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

An exact `[Silent]` assistant response does not emit a completion notification.

The iOS app controls which categories are enabled, whether notification previews are shown, and whether completion sounds play.

## Privacy and security

- Pairing creates a revocable, profile-scoped credential.
- The credential is stored in the profile-aware Hermes home as `conduit-push.json` with mode `0600` on supported systems.
- Authorization credentials are never written to logs or printed by `status`.
- Hook callbacks enqueue bounded events; HTTPS delivery runs on a background worker and does not block the agent loop.
- Event titles and bodies are length-limited before delivery.
- Lock Screen previews are disabled by default in Hermes Conduit.

The public relay URL is part of the client protocol. APNs signing material (the `.p8` key) is never committed — see the relay directory below for how to deploy your own.

## Push relay

The push relay is the server component that receives events from Hermes gateways and delivers them to iOS devices via APNs. The source lives in [`relay/`](relay/).

**Architecture:** Hermes plugin → HTTPS → relay → APNs → Conduit app

The relay handles device registration, pairing codes, per-installation preferences, rate limiting, and idempotent event delivery. It uses only the Node.js standard library (no npm dependencies).

### Self-hosting

```shell
cd relay/deploy

# 1. Copy and fill in environment
cp .env.example .env
# Edit .env with your PUBLIC_URL and Apple developer credentials

# 2. Place your APNs signing key
mkdir -p secrets
cp /path/to/AuthKey_XXXXXX.p8 secrets/apns-production.p8

# 3. Build and run
docker compose up -d
```

The relay listens on port 9120. Put it behind an HTTPS reverse proxy (the relay validates that `PUBLIC_URL` uses HTTPS). If the proxy sends `X-Forwarded-For`, set `TRUST_PROXY=1`.

**What stays secret:**

| File | Contents | Gitignored |
|------|----------|------------|
| `.env` | APNs key ID, team ID, topic, public URL | Yes |
| `secrets/*.p8` | Apple Push Notification signing key | Yes |
| `data/relay.json` | Installation records, gateway credentials | Yes |

See [`relay/deploy/.env.example`](relay/deploy/.env.example) for all required environment variables.

### Relay API

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/healthz` | Health check |
| POST | `/v1/installations` | Register a device |
| PUT | `/v1/installations/:id` | Update device token / preferences |
| DELETE | `/v1/installations/:id` | Deactivate a device |
| POST | `/v1/installations/:id/pairings` | Create a pairing code |
| POST | `/v1/pairings/claim` | Claim a pairing code (gateway side) |
| POST | `/v1/events` | Deliver a notification event |
| DELETE | `/v1/gateways/current` | Revoke a gateway credential |

## Conduit support and privacy

The repository also hosts the public Hermes Conduit support and privacy pages:

- [Hermes Conduit support](https://kaishi00.github.io/hermes-conduit-notifier/support/)
- [Hermes Conduit privacy policy](https://kaishi00.github.io/hermes-conduit-notifier/privacy/)

The static site source lives in [`docs/`](docs/).

## Development

The runtime uses only the Python standard library plus Hermes APIs. Run the pure event tests with:

```bash
python -m pytest --rootdir=tests tests
```

## License

MIT
