# Hermes Conduit Notifier

Hermes Conduit Notifier is the open-source Hermes plugin that connects a Hermes host to the [Hermes Conduit](https://github.com/kaishi00/hermes-conduit) iPhone, iPad and Apple Watch app. It started as the notification sender and now carries most of what Conduit needs from the host:

- **Notifications and calls**: approval, input, reply, failure and background-task notifications, Hermes calling you (CallKit on the phone and Watch), and Lock Screen progress while a turn runs, all delivered through the Conduit push relay in [`relay/`](relay/), end-to-end encrypted where APNs allows.
- **Live voice**: single-use Gemini Live tokens, temporary Qwen Live credentials, GPT-Live sessions on the host's ChatGPT subscription or API key, Grok Live relayed with the host's xAI sign-in, Local Live and Classic voice through your own speech server, voice web lookups, memory and personality for voice, and saved call transcripts.
- **Apple Watch**: short-lived grants that let a Watch call run lookups and Hermes jobs, answer what Hermes waits on in a call from Hermes, and stream GPT-Live, Grok and Local Live audio, through the relay, sealed end to end.
- **Phone data the user opts into**: Apple Health, location, maps, calendar, reminders, Focus, phone status, contacts and alarms, sent by the phone straight to this host and sealed end to end, with the `conduit_health`, `conduit_location`, `conduit_maps`, `conduit_calendar`, `conduit_phone` and `conduit_contacts` tools for the agent, and events from the user's Shortcuts automations through `conduit_inbox`.
- **Conduit extras**: rich cards and on-screen cards during calls, Artifacts, group pictures, chat takeover, cron notification settings, Conduit settings the agent can change, and one-tap pairing and updates from the app.

Every route sits behind the Hermes dashboard's own auth. The plugin holds no Apple Push Notification service key and no dashboard credentials: Apple credentials stay on the push relay, so self-hosted users never need to copy a shared signing key onto their gateway, and provider keys and sign-ins never leave this host. What each party can see is in the [privacy policy](https://kaishi00.github.io/hermes-conduit-notifier/privacy/).

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

With plugin 0.24+ Conduit pairs the active dashboard's profile by itself: it
creates the code and hands it to `POST /api/plugins/conduit_push/pair?profile=`
over your signed-in dashboard connection (see [Pairing from Conduit](#pairing-from-conduit-plugin-024)).

## Manage the pairing

```bash
# Show status without printing the credential
hermes conduit-push status

# Send a local test event through the relay
hermes conduit-push test

# Redact chat text from pushes (see Privacy and security)
hermes conduit-push redact on

# Revoke this profile's credential
hermes conduit-push unpair
```

Update the plugin and restart Hermes:

```bash
hermes plugins update conduit_push
hermes gateway restart
```

The dashboard imports the plugin's routes once, so restart the dashboard as
well (or let Conduit do it: Settings > Conduit plugin > Update runs Hermes' own
plugin update, the gateway restart and [the dashboard restart](#updating-from-conduit-plugin-024)).

### On the Hermes dashboard (plugin 0.74+)

The dashboard's Sessions page shows a Conduit card at the top, once Conduit
has used this host. Hermes' own Connected Platforms card lists only the
gateway's messaging adapters (Matrix, the API server), and Conduit talks to
the dashboard instead, so it gets its own card:

- **App**: Connected while a Conduit gateway socket that opened a chat is
  open, or within 5 minutes of Conduit's last request to this plugin; Idle
  after that, with when it was last seen.
- **Push notifications**: Delivering (with the last push's time), Failing
  (with a short reason: the relay unreachable, its answer, a certificate),
  Refused or Blocked (the same marks `status` reports), Paired with nothing
  sent yet, or Not paired.

It reads `GET /api/plugins/conduit_push/link-status` every 30 seconds. The
times live in `conduit-link-status.json` in the profile's Hermes home, written
at most once a minute each; it holds no credential or relay address.

## Events

The plugin currently emits notifications for:

- approval needed
- clarification or other input needed
- response ready, once the turn has ended: a turn that failed sends only its failure notice, not Hermes' error text as a reply (plugin 0.35+)
- failed turns
- completed background tasks: a delegated task that ends while the turn that started it is still running is part of that turn's reply and sends nothing of its own (plugin 0.35+)
- calendar and reminder changes Hermes asks your phone to make (see [Calendar, Reminders, Focus and phone status](#calendar-reminders-focus-and-phone-status-from-conduit-plugin-027)): a notification to approve, or a silent wake for adds you let Hermes make without asking
- call requests: a job you asked to be called about has ended, Hermes asked to call you, or (alert calls) Hermes has waited a minute on your approval or answer, or a turn failed (see [Hermes calls you](#hermes-calls-you-plugin-013))

An exact `[Silent]` assistant response does not emit a completion notification. Reply and task bodies are sent as plain text: Markdown fences, emphasis, headings, link targets and table rules are dropped (plugin 0.35+). No notification shows a file's path on the host (plugin 0.71.2+): a `MEDIA:` tag in a reply, a question, an approval, a call or a phone change, and in the answer card or call screen that comes with it, reads as 📎 and the file's name, or its type (`📎 PPTX`) when the name is only a hash, and `[[audio_as_voice]]` and `[[as_document]]` are dropped. Code in a reply's fenced block stays as written.

### Cron runs (plugin 0.33+)

A cron run notifies like any other turn, with two exceptions:

- A run whose reply is Hermes' cron silence marker doesn't notify, by the same rule Hermes uses to skip delivering it: `[SILENT]` (or `SILENT` / `NO_REPLY`) as the whole reply, on its own first or last line, or opening the reply (`[SILENT] No changes since yesterday`). A marker mid-sentence is real content and still notifies.
- You can turn a job's notifications off in Conduit's Cron tab. Its runs then send no reply or failure notification (and no automatic alert call; a call you asked for on the job still rings). The muted job ids live in the profile's `conduit-cron.json`; `GET /api/plugins/conduit_push/cron/notifications` lists them (`{"ok": true, "muted": [...]}`) and `PUT` with `{"job_id": "...", "notify": false}` changes one. Capability `cron-notifications`.

The plugin tells a cron run by its task id (`cron:<job id>:…`) or its classic session id (`cron_<job id>_<date>_<time>`).

Beside notifications, the plugin keeps a Live Activity up to date while a turn you sent from Conduit runs (see [Lock Screen progress](#lock-screen-progress-plugin-032)).

Each notification is titled with the session's title when it has one (for example a Telegram topic name), so you can tell which chat it came from: a reply shows the session title, and the others prefix it (`Approval needed · Infra`). A Conduit Bot Chat, whose title is always `Bot Chat`, is titled with its bot's name instead, as Conduit's Bots list shows it (its Bot Mode title, else its display name, else its profile name), so you can tell which bot replied. Every push also carries that name, so a call from the bot names it too. Untitled sessions keep the generic copy. With previews off the title is not shown; `redact on` or end-to-end encryption keeps it from reaching the relay.

The iOS app controls which categories are enabled, whether notification previews are shown, whether completion sounds play, and whether approval and input-needed notifications play a sound (on by default).

## Privacy and security

- Pairing creates a revocable, profile-scoped credential.
- The credential is stored in the profile-aware Hermes home as `conduit-push.json` with mode `0600` on supported systems.
- Authorization credentials are never written to logs or printed by `status`.
- Hook callbacks enqueue bounded events; HTTPS delivery runs on a background worker and does not block the agent loop. Two waits are bounded instead (plugin 0.81.2+), for a process that exits right after its turn, such as a restart-safe cron job's worker: a turn end that sends a call request waits up to 3 seconds for it to reach the relay, and the process's exit waits up to 3 seconds for queued notifications and up to 10 for call requests still retrying (call requests first, so a fallback push they queue is among those notifications).
- A delivery that fails on the way (no connection, a timeout, `408`, `429` or a `5xx` from the relay) is sent again with the same event ID after 1, 2 and 4 seconds, for at most about 30 seconds per event; the relay drops the repeats it already accepted. Other refusals are not retried. A `401` means the relay no longer knows this gateway (Conduit removed it, or the relay lost its data): the pairing is kept, and `status` says to pair again from Conduit until a delivery is accepted again. A `401` or `403` also parks notifications for that pairing (plugin 0.55.1+): events are dropped for 1 minute, doubling with each refusal in a row up to 30 minutes, then the next event goes out as the probe and an accepted one (or an accepted `test`) ends the pause; a clarify question asked meanwhile uses the native prompt at once (Live Activity reports pause 30 minutes on their own). A `403` with no JSON body comes from something in front of the relay (a firewall or rate limiter) blocking this host's address: `status` says so instead of telling you to pair again, since a pairing claim meets the same block, and Conduit's pairing status reports it as `relay_blocked`.
- Disabling or reloading the plugin (on a Hermes with plugin unload callbacks) stops its background work: notifications still queued and replies waiting for their turn's end are dropped, the delivery and Lock Screen threads end, and a call held until your call ends is kept for the next load instead of going out.
- The health, location, maps, calendar, phone, contacts, calls, display and inbox (`conduit_inbox`) tools refuse delegated subagents, including one whose session id a compaction replaced (`compression.in_place: false`): the plugin asks Hermes' delegation flag (Hermes 0.22+) and follows the compaction links in Hermes' session store back to the subagent it was (plugin 0.51.6+).
- Event titles and bodies are length-limited before delivery.
- Lock Screen previews are disabled by default in Hermes Conduit.
- **End-to-end encryption** (plugin 0.5+, relay 0.5+, a Conduit build with encrypted notifications): Conduit creates a secret for each pairing on the phone and hands it to this profile over your Hermes dashboard connection, so it never passes through the relay. From then on every notification's title, body, profile (and the bot's name), session and decision card is sealed with it (ChaCha20-Poly1305, keys derived with HKDF-SHA256), the phone decrypts it in a Notification Service Extension, and clarify answers come back sealed for this profile. The relay and APNs see only routing data: the event type, opaque ids, a keyed thread token and timing. A pairing that has a key never sends plaintext content again, and only accepts clarify answers sealed by the phone. `hermes conduit-push status` shows whether it's on; Conduit turns it on by itself when it connects to this host. Run `hermes conduit-push test --corrupt` to check that the phone shows only generic text for a push it can't verify. The secret is only as private in transit as your dashboard connection (HTTPS or a private tunnel is recommended). Keys rotate (plugin 0.71+ and a Conduit build that rotates): Conduit hands over a new secret when the key is 30 days old (7 days with plugin 0.72+), when you tap Rotate in Conduit's Settings > Notifications, or after `hermes conduit-push e2e rotate` on this host, the next time it connects to the dashboard. Pushes are sealed with the new key at once; the old one still opens clarify answers for 7 days, so nothing in flight is lost. For a key that may have leaked, `hermes conduit-push e2e rotate --now` skips that overlap; the current key still seals notifications until Conduit next connects, so `hermes conduit-push unpair` is the way to cut it off at once. A key copied from a backup then stops opening new notifications, though it can still open ones captured while it was in use. From plugin 0.72 Conduit rotates inline instead, every 7 days (`POST /e2e/rotate`): its request, sealed with the current key, carries a fresh X25519 share and, on iOS 26 and later, an ML-KEM-768 encapsulation key; this profile answers with its share and the ML-KEM ciphertext, and both derive the new secret from the exchange mixed with the old secret (HKDF-SHA256). The new secret never crosses the dashboard connection, so only a pairing's first key depends on it, and with ML-KEM even a recording of that first handover can't open notifications after the first rotation. `hermes conduit-push status` says which kind of key is in use. ML-KEM needs `cryptography` with ML-KEM support (version 47 or later, built against OpenSSL 3.5); without it the profile offers X25519 only. To make the first handover quantum-safe too, let your HTTPS front end offer the hybrid TLS group X25519MLKEM768 (nginx 1.25+ with OpenSSL 3.5+: `ssl_ecdh_curve X25519MLKEM768:X25519:secp256r1:secp384r1;`); iOS 26 uses it automatically.
- `hermes conduit-push redact on` keeps chat content from leaving the gateway: events carry no title or body text, approval cards say "Hermes needs your approval. Open Conduit for details.", and clarify cards replace question text with generic copy. The fields needed to answer stay (session key or request id, question ids, choice labels), so cards raised while the app is backgrounded remain recoverable and answerable. Clarify choice labels still transit the relay because the answer is one of them. `status` shows the current setting; `redact off` restores full content, including for events still waiting in the local delivery queue.

- Besides notifications, the relay carries call pushes (sealed like notifications, except the "stopped ringing" notice), Lock Screen progress (phase, a tool category from a fixed list, tool count and context percentage, never chat text; `redact on` drops the category), Apple Watch grants and audio (sealed per call), the one-time codes that prove a phone holds its APNs token, and short-lived Apple Maps tokens for the host maps engine (the relay never sees the lookups). Health, location, calendar, phone and contacts data never pass through it.

The public relay URL is part of the client protocol. APNs signing material (the `.p8` key) is never committed — see the relay directory below for how to deploy your own.

## Push relay

The push relay is the server component that receives events from Hermes gateways and delivers them to iOS devices via APNs. The source lives in [`relay/`](relay/).

**Architecture:** Hermes plugin → HTTPS → relay → APNs → Conduit app

The relay handles device registration, pairing codes, per-installation preferences, rate limiting, and idempotent event delivery. It uses only the Node.js standard library (no npm dependencies).

### Self-hosting

**A self-hosted relay only works with a Conduit build you sign yourself.** APNs only accepts notifications for the App Store app (`com.milim.relay`) when they are signed with a key from its developer team, so a relay running anyone else's key can't reach the App Store build, even if you enter its address under Settings > Notifications > Push relay. To self-host end to end you need your own Apple Developer account, a build of [Hermes Conduit](https://github.com/kaishi00/hermes-conduit) under your own bundle id, and an APNs key for that bundle id in the relay below (`APNS_TOPIC` must be your bundle id). If you only want the relay operator to stop seeing your content, you don't need to self-host: with end-to-end encryption on (see Privacy and security) the shared relay only sees routing data.

```shell
cd relay/deploy

# 1. Copy and fill in environment
cp .env.example .env
# Edit .env with your PUBLIC_URL and Apple developer credentials

# 2. Place your APNs signing key
mkdir -p secrets
cp /path/to/AuthKey_XXXXXX.p8 secrets/apns-production.p8

# 3. Create the data directory, owned by the image's node user (uid 1000)
mkdir -p data
sudo chown 1000:1000 data

# 4. Build and run
docker compose up -d
```

Skip step 3 and Docker creates `data/` owned by root on a Linux host. The relay runs as `node`, can't write there, and exits at startup naming the directory. Docker Desktop on macOS maps ownership through its file sharing, which hides this.

The relay listens on port 9120. Put it behind an HTTPS reverse proxy (the relay validates that `PUBLIC_URL` uses HTTPS). If the proxy sends `X-Forwarded-For`, set `TRUST_PROXY` to the number of proxies in front of the relay (`1` for one): the relay rate-limits by the address the outermost trusted proxy appended, never by an entry the client sent itself. That holds only when every request comes through the proxy, so don't publish port 9120 or share its network with anything else. `APNS_MODE`, `APNS_CAPTURE_PATH` and `APNS_VOIP_FAILURE` are test settings; a relay refuses to start with them.

The plugin sends relay credentials only over HTTPS with a trusted certificate (plain HTTP only to the same machine), and never follows a redirect. You can pair over plain HTTP against a LAN or Tailscale address, for example `hermes conduit-push pair XXXXX-XXXXX --relay-url http://100.x.y.z:9120`: the relay answers with its HTTPS `PUBLIC_URL`, and the plugin saves and uses that from then on. Pair that way only on a network you trust: whatever answers on that address chooses the HTTPS relay this host uses from then on.

**What stays secret:**

| File | Contents | Gitignored |
|------|----------|------------|
| `.env` | APNs key ID, team ID, topic, public URL | Yes |
| `secrets/*.p8` | Apple Push Notification signing key | Yes |
| `data/relay.json` | Installation records, gateway credentials | Yes |

See [`relay/deploy/.env.example`](relay/deploy/.env.example) for all required environment variables.

Check retained installation capacity without changing the data file. From the
repository root, pass the mounted JSON path directly:

```shell
node relay/src/storage-status.mjs relay/deploy/data/relay.json
```

Or, from `relay/deploy`, inspect the running Compose service:

```shell
docker compose exec conduit-push npm run --silent storage:status
```

The command prints only total, active, inactive, and maximum installation
counts. Inactive installations remain in the store and still consume slots:
automatic APNs deactivation and repeated uninstall/reinstall cycles count
toward the installation limit (50,000 by default, `RELAY_MAX_INSTALLATIONS`),
which the printed maximum reflects. Review counts before scheduling offline data
maintenance. `DELETE /v1/installations/:id` deactivates an installation and
does not free its slot right away. When the relay is full, a new
registration takes the slot of the longest-inactive installation (with its
pairing codes and parked decisions): an inactive installation can never be
used again, since its credential no longer authenticates and its hosts'
events are refused. Only a relay full of active installations refuses new
ones.

For offline reclamation, `storage:prune-inactive` previews the number of
eligible records by default. It removes only installations that have been
inactive and unchanged for at least 30 days, have no gateway credentials,
and have no pairing or pending-decision references. Active, recent, legacy
gateway-bound, and referenced records are preserved; other store sections
are left unchanged. Malformed state causes a generic failure without writes.

From `relay/deploy`, stop every relay writer before previewing or applying:

```shell
docker compose stop conduit-push
docker compose run --rm --no-deps conduit-push npm run --silent storage:prune-inactive
```

Review the counts, then explicitly apply and restart:

```shell
docker compose run --rm --no-deps conduit-push npm run --silent storage:prune-inactive -- --apply --relay-stopped
docker compose start conduit-push
```

The command creates an exclusive, exact-byte `relay.json.backup-*` beside
the data file before atomically replacing it. Backups contain credentials
and private messages: protect them like the original data file. The
`--relay-stopped` flag acknowledges the offline requirement; it does not
stop running writers. A byte comparison detects changes before replacement,
but cannot make concurrent maintenance safe. Preview and zero-eligible runs
create no backup and change no files. For a direct Node invocation, use
`node relay/src/storage-prune-inactive.mjs PATH_TO_RELAY_JSON` and the same
explicit flags when applying. Reclamation is operator-run, never automatic;
gateway-bound inactive records still require separately reviewed maintenance.

### Relay API

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/healthz` | Health check |
| POST | `/v1/token-challenges` | Push a one-time registration nonce to `device_token` (`bundle_id`, `environment` as for registering); 0.16+ |
| POST | `/v1/installations` | Register a device; `token_proof` carries the pushed nonce (see "Registration proof") |
| PUT | `/v1/installations/:id` | Update device token (a new one needs `token_proof`), PushKit token (`voip_token`, `null` clears it), the paired Apple Watch's PushKit token (`watch_voip_token`, 0.10+), location push token (`location_token`, 0.12+) / preferences |
| DELETE | `/v1/installations/:id` | Deactivate a device |
| POST | `/v1/installations/:id/pairings` | Create a pairing code |
| POST | `/v1/pairings/claim` | Claim a pairing code (gateway side) |
| POST | `/v1/events` | Deliver a notification event |
| DELETE | `/v1/gateways/current` | Revoke a gateway credential |
| PUT / DELETE | `/v1/installations/:id/live-activities` | Register (`push_token`, `session_ids`) or remove (`push_token`) the Live Activity the phone started for a chat (0.15+) |
| POST | `/v1/live-activity` | Report a running turn's progress (`session_id`, `state`, optional `end`); answers `tracked` (gateway side, 0.15+) |
| POST | `/v1/watch-tools/grants` | Open a Watch tool grant for one call (gateway side) |
| GET | `/v1/watch-tools/grants/:id/calls` | Long-poll for the grant's Watch tool calls (gateway side) |
| POST | `/v1/watch-tools/grants/:id/results/:rid` | Answer one Watch tool call (gateway side) |
| POST | `/v1/watch-tools/grants/:id/calls` | A Watch tool call, held open until the gateway answers (Watch side, the grant's relay key) |
| DELETE | `/v1/watch-tools/grants/:id` | Close a grant (either side; always 204, so a wrong key learns nothing) |
| GET (WebSocket) | `/v1/watch-audio/:id/host` | The gateway's side of a Watch call's audio (a grant opened with `audio: true`) |
| GET (WebSocket) | `/v1/watch-audio/:id/watch` | The Watch's side of it (the grant's relay key) |
| POST | `/v1/rings/:id/settled` | The phone or the Watch answered or declined a call that rings on both (0.10+; the ring's token from the call push) |
| PUT | `/v1/rings/:id/session` | The phone stores the sealed session of a call the Watch answered (0.11+; the ring's token) |
| GET | `/v1/rings/:id/session` | The Watch fetches it, waiting up to `?wait=20` seconds (0.11+; the ring's token as the bearer) |
| POST | `/v1/rings/:id/control` | The phone or the Watch mutes, unmutes or ends a call the Watch answered that stays on the phone's headset (0.17+; the ring's token) |
| GET | `/v1/rings/:id/control` | Either reads the other's change past `?after=` its last seq, waiting up to `?wait=20` seconds (0.17+; the ring's token as the bearer) |

Watch tool grants carry an Apple Watch call's lookups while the Watch can't
reach Conduit on the iPhone. Calls and answers are sealed with a per-call key
the relay never sees (ChaCha20-Poly1305, bound to the grant, call id and
direction), so the relay only forwards ciphertext. Grants live in memory,
last at most 30 minutes and 120 calls, and a restart drops them; the Watch
then falls back to the iPhone. Revoking a gateway, or deactivating its
installation, closes its grants at once, with their waiting calls, host polls
and audio bridges. A relay holding its maximum of grants answers a
new one with 503 `watch_grant_capacity`, and that call's lookups go through
the iPhone.

A grant opened with `audio: true` (relay 0.7+) can also carry the call's live
audio for GPT-Live and Grok on the Watch: the gateway and the Watch each open
a WebSocket, and the relay copies binary messages between them. They are
sealed end to end like the tool calls; the relay reads only a message's first
byte, which is never 0 (its own two-byte notices to the gateway: Watch
connected, Watch gone). Limits per socket: 64 KB a message, 96 KB/s averaged
over 5 s, 60 s of silence; bridges are capped per gateway and per relay (see
below).

A call rings on the Apple Watch too (relay 0.10+) when the phone registered the
Watch's own PushKit token: iOS doesn't pass a calling app's call to the Watch,
so the relay sends the call push to both, with topic `APNS_WATCH_TOPIC`
(default `<APNS_TOPIC>.watchkitapp`, then `.voip`). Both pushes carry the same
`ring`: an id, a token that only those pushes hold, and the `url` to settle it
at, since the Watch knows no relay address of its own. The device that answers
or declines posts `{token, by: "phone"|"watch", outcome: "answered"|"declined"}`
to `/v1/rings/:id/settled`, and the relay sends the other device a VoIP push
`{conduit: {ring: {id, settled, by}}}` that expires after a minute, so it stops
ringing. The first settle wins; a later one gets 409 with how the ring was
settled, and an unknown ring or wrong token gets 404. Rings live in memory for
10 minutes, and a restart drops them: the other device then rings out. A Watch
token APNs refuses as gone is forgotten, and the phone still rings. A Watch
push refused with `DeviceTokenNotForTopic` keeps the token and logs
`check APNS_WATCH_TOPIC`: the setting is wrong, not the token. The `.p8` key
must be allowed to push to the Watch app's bundle id as well as the iPhone
app's: a team-scoped key is, but a topic-specific key that lists only the
iPhone app gets `TopicDisallowed` for every Watch push. The relay then keeps
the token and logs `apns key not allowed for this topic`. A Watch push that
APNs accepts logs `watch voip sent`.

A stop push that doesn't land isn't sent again: the other device rings out on
its own. The settle answer's `notified` says only that the other device has a
PushKit token at the relay, so a stop push went to it, not that it arrived.

A call answered on the Watch starts through the relay too (0.11+): the Watch's
link to the iPhone stays down under the system call screen, so it can't ask
Conduit there for the call's session. Its settle carries a sealed `start`,
which the relay forwards untouched in the phone's stop push (`ring.start`).
Conduit builds the session as for any Watch call and stores it, sealed, with
`PUT /v1/rings/:id/session {token, sealed}`; the Watch fetches it with
`GET /v1/rings/:id/session?wait=20` and the ring's token as the bearer (200
with `sealed`, or 204 when nothing came in time). Both are sealed with a key
only the phone and the Watch hold, so the relay sees their size and nothing
else. A session can be stored only for a ring the Watch answered (409
otherwise), lasts 2 minutes in memory, and is at most 96 KB; the relay holds
32 MB of them at most, dropping the oldest first, and a ring takes two waiting
fetches at a time. An older relay ignores `start`, and the Watch then starts
the call over its link to the iPhone, as before.

A call the Watch answers while the phone has a headset on (AirPods) stays on
the phone, so its voice plays there as a phone call's does, and the Watch's
call screen keeps Mute and End. The phone answers its own ring, says so in the
session it stores (`callOnPhone`), and the two share the call's mute and end
through `/v1/rings/:id/control`: either posts `{token, by, muted?, ended?}`,
and each long-polls `GET ?after=<seq>&wait=20` for the other's (200 with
`{seq, muted, ended, by}`, or 204 when nothing changed in time). Only those
flags go through. A control exists only for a ring the Watch answered (409
otherwise); a call once ended stays ended, and a ring takes four waiting reads
at a time. Each read or write keeps the ring another 10 minutes, up to 4 hours
after it rang. A restart drops rings, so a write with `adopt: true` brings an
unknown ring back as a control alone, with the token of the first write: the
id is what an adoption trusts. Only the call's two devices know a ring's id,
which the relay's logs leave out. Each device
whose read comes back 404 writes its state again with it and reads on from
`after=0` (an adopted ring counts from 1 again), and a
change made while the relay was down is retried. Adopted rings are capped at
1,000, apart from ringing calls, which they never push out. On a relay older
than 0.17 the control is 404, and the Watch takes the call as before.

### Registration proof

A phone proves it holds the APNs device token it registers. It asks
`POST /v1/token-challenges` for a challenge, the relay sends a silent push
carrying a one-time nonce (`conduit_token_challenge`) to that token, and the
phone sends the nonce back as `token_proof` with `POST /v1/installations`, or
with a `PUT` that moves the installation to a new device token. A nonce lasts
10 minutes and works once. Without a proof the relay answers `403`
`token_proof_required`; a wrong or expired one gets `token_proof_invalid`.

So a token that leaks (a data-file backup, a log) can't be bound to someone
else's installation, and filling the relay's installation slots takes real
phones running the app rather than a script. A proven registration retires
the same phone's earlier installations for that token. PushKit and location
tokens can't be challenged this way, so a registration or update naming one
that another active installation already holds is accepted without it: a
registration gets none (`voip`, `watch_voip` or `location` comes back
`false`), and an update keeps the token the installation had.

A challenge answers `400 invalid_device_token` when APNs refuses the token,
so it shows whether a token is currently live for the app. The nonce never
reaches whoever asked, so that grants nothing else, and per-token, per-IP
and relay-wide limits bound such probing.

`RELAY_TOKEN_PROOF` is `required` by default. `optional` still accepts
registrations and token changes without a proof (a proof that is sent must
be valid): use it only while app builds from before token proof still need
to register. Installations registered before token proof keep working
either way; they need a proof only to move to a new device token.

### Capacity and admission limits

The defaults are sized for the shared public relay with thousands of
installations. Each bound is a backstop against floods, not a quota ordinary
traffic reaches. Every one can be changed with the environment variable next
to it: unset or empty keeps the default, and anything other than a positive
integer stops the relay at boot.

| Bound | Default | Variable |
|-------|---------|----------|
| Installations, inactive ones included | 50,000 | `RELAY_MAX_INSTALLATIONS` |
| Gateways per installation | 64 | `RELAY_MAX_GATEWAYS_PER_INSTALLATION` |
| Event IDs remembered per installation | 5,000 | `RELAY_MAX_EVENT_IDS_PER_INSTALLATION` |
| Event IDs remembered across the relay | 250,000 | `RELAY_MAX_EVENT_IDS` |
| Active clarify decisions across the relay | 1,024 | `RELAY_MAX_ACTIVE_DECISIONS` |
| Retained clarify decisions across the relay | 4,096 | `RELAY_MAX_RETAINED_DECISIONS` |
| Registrations per minute | 24 | `RELAY_REGISTRATIONS_PER_MINUTE` |
| Registration challenges per minute (each is a push; also 5 per device token per 10 minutes and 12 per IP address per minute, fixed) | 240 | `RELAY_TOKEN_CHALLENGES_PER_MINUTE` |
| Events per minute | 6,000 | `RELAY_EVENTS_PER_MINUTE` |
| Device updates that change state, pairing creations, and valid claims per minute | 240 | `RELAY_DEVICE_CHANGES_PER_MINUTE` |
| Decision answers and releases that change state per minute | 600 | `RELAY_DECISION_ACTIONS_PER_MINUTE` |
| Installation deactivations and gateway revocations per minute | 24 | `RELAY_REVOCATIONS_PER_MINUTE` |
| Live Watch tool grants across the relay | 2,048 | `RELAY_MAX_WATCH_GRANTS` |
| Live Watch tool grants per gateway (a new one closes the oldest) | 4 | `RELAY_MAX_WATCH_GRANTS_PER_GATEWAY` |
| Live Watch audio bridges across the relay (GPT-Live and Grok on the Watch) | 200 | `RELAY_MAX_WATCH_AUDIO_BRIDGES` |
| Live Watch audio bridges per gateway | 2 | `RELAY_MAX_WATCH_AUDIO_BRIDGES_PER_GATEWAY` |
| Watch tool calls per minute | 1,200 | `RELAY_WATCH_CALLS_PER_MINUTE` |
| Hermes call requests per installation per day (each new request counts, even one that rings no device) | 60 | `RELAY_CALLS_PER_INSTALLATION_PER_DAY` |
| Live Activities held in memory, relay-wide / per phone (the oldest is forgotten past either) | 50,000 / 8 | `RELAY_MAX_LIVE_ACTIVITIES` / `RELAY_MAX_LIVE_ACTIVITIES_PER_INSTALLATION` |
| Live Activity registrations per minute, relay-wide | 3,000 | `RELAY_LIVE_ACTIVITY_REGISTRATIONS_PER_MINUTE` |
| Live Activity reports for a watched turn per minute, relay-wide | 30,000 | `RELAY_LIVE_ACTIVITY_UPDATES_PER_MINUTE` |
| Live Activity registrations and removals per phone / per IP address, per minute (fixed) | 60 / 120 | |

Event IDs only prevent a repeated delivery of the same event within 24 hours,
so the relay keeps them in memory and never writes them to the data file. A
restart forgets them, which costs at most one duplicate push; replaying an
event after a restart needs that gateway's credential, which can send new
events anyway. An event whose push fails in a way a retry can fix (APNs
refused or unreachable, an hourly limit) gives its ID back, so the plugin's
retry is sent; a push APNs took but whose answer was lost can therefore show
twice. A clarify decision is the exception: it keeps its ID, because a retry
can't re-park it, and the plugin falls back to Hermes' own clarify path. An
installation at its event-ID bound forgets its own oldest ID, and at the
relay-wide bound the installation holding the most IDs forgets its oldest, so
neither bound rejects an event. A gateway's last-seen time and plugin version
are written at most every five seconds, and on shutdown, instead of on every
event. Each of those writes is still a full rewrite of the data file, so a busy
relay spends one whole-file write per five seconds on them rather than two per
event.

Installations, gateways, and clarify decisions are persistent. A new record is
rejected with HTTP 429 when its bound is full; the relay never evicts existing
credentials. A store already above a limit when upgraded keeps every record,
but cannot add records of that kind until the operator raises the limit or
removes records while the relay is stopped. Re-pairing a profile adds a
gateway without revoking the previous one, which is why the gateway bound is
generous.

The per-minute budgets are process-wide and separate for each class of
action, so a flood of one kind cannot starve the others. Validation, capacity
rejection, and per-client limits run before a budget is charged. Unchanged
device updates, already-accepted events without plugin metadata, non-mutating
decision responses, and repeated cancellations use no budget and do not
rewrite the store; health checks and decision polling use none either. The
per-installation limits of 30 events and 30 device updates per minute still
apply.

Run exactly one relay process/replica per `DATA_PATH`. The relay caches its
state and rewrites the whole JSON file on every persistent change; atomic
replacement does not coordinate multiple writers, and sharing the file
between processes can lose updates. The budgets are per process. A hostile
flood can still saturate a budget or a bound, and installation IDs alone
cannot establish fairness between actors.

## Rooms mailbox

The rooms mailbox in [`mailbox/`](mailbox/) is a small store-and-forward service for linked rooms: agents on Hermes installations owned by different people, taking part in one room. Two installations that can't reach each other leave sealed envelopes in each other's inbox there, using outbound requests only, and the mailbox never sees what the envelopes say. The plugin links installations through it (see Linking with someone, below), and rooms go through those Links with the room bridge, in a later release. See [`mailbox/README.md`](mailbox/README.md) for its protocol, limits and self-hosting.

## Lock Screen progress (plugin 0.32+)

When you send a message from Conduit, it starts a Live Activity on the Lock
Screen and in the Dynamic Island: what Hermes is doing (thinking, running a
kind of tool, waiting for your approval or answer), how long the turn has run,
how many tools it used and how full its context is. Conduit updates it itself
while it is open; once you leave it, the plugin keeps it current through the
relay (relay 0.15+), and the end of the turn ends it.

- The phone registers the activity's push token with the relay under the
  chat's session ids. The plugin reports each turn it runs: phase changes,
  tool starts, approvals and clarify questions, and each model request's
  context use (`pre_api_request` / `post_api_request`, which older Hermes
  versions may not have; the rest still works without them).
- Reports are coalesced: at most one every 5 seconds per watched turn, the end
  at once. The relay answers whether the phone is watching; a turn nobody
  watches is asked about again only every 15 seconds, and a relay without the
  route turns reporting off for half an hour.
- Live Activity payloads are rendered by iOS itself, so they can't be sealed
  end to end like notifications. What reaches the relay and APNs is coarse on
  purpose: the session id, a phase, a tool category from a fixed list
  (`terminal`, `files`, `web`, `browser`, `connector`…, never a tool's name,
  arguments or output), a tool count and a context percentage. The chat's
  title stays on the phone. With `redact on` the tool category is left out
  too.
- The relay keeps registrations in memory only (at most 8 hours, the longest
  iOS runs an activity). A relay restart stops updates; the activity then
  shows as out of date on the phone and Conduit ends it when you open it.

## Batch clarify decisions (plugin 0.3+)

Current Hermes lets one `clarify` call ask several questions
(`questions: [{qid, question, choices, multi_select}]`). The plugin relays
the FULL batch to Conduit instead of collapsing it to the first question:

- The pushed decision carries `questions[]` with the gateway qids, choices,
  and `multi_select` flags. The old collapsed `question`/`choices` summary
  still rides along, so pre-0.3 Conduit builds keep rendering an answerable
  first-question card.
- The relay stores per-question answers with **first-answer-wins per qid**:
  two devices answering the same qid resolve to one lock (the loser gets a
  409), and the decision completes only when every qid is locked.
- Devices answer per question with
  `POST /v1/decisions/:id/respond {"question_id": "q…", "answer": "…"}`
  and receive the remaining open qids back (`POST /v1/decisions/:id` is
  kept as a backward-compatible alias running the same handler). The
  legacy whole-decision body (`{"answer": "…"}`) still works for
  single-question cards, and on a batch it counts as the collapsed first
  question only.
- A question can be left blank, as Conduit's form leaves a field the user
  didn't fill: a blank `answer` with its `question_id` locks that question
  with an empty answer, which Hermes reads as skipped (relay 0.18,
  capability `clarify-skip`; an encrypted pairing seals the empty answer
  like any other). A blank answer without a question, or for a decision
  with no `questions[]`, is `400 invalid_answer`.
- Duplicate qid answers and released decisions are distinct outcomes:
  `409 already_answered` settles only that qid as answered elsewhere,
  while `410 decision_released` (the native Desktop/TUI path resolved the
  whole clarify) tells Conduit to retire the entire pushed card. An
  unknown qid on a live decision is `400 invalid_question_id`, not a
  missing decision.
- An answer is kept whole or refused, never cut: plaintext is trimmed and
  may be up to 2,000 characters, a sealed answer (`e2e1.…`, encrypted
  pairings) up to 11,000. A longer one is `413 answer_too_large` and the
  question stays open; a sealed answer too short to hold its tag is
  `400 invalid_answer`.
- The structured decision and the notification are independent: when the
  card cannot be delivered (decision_cards disabled, or an oversized batch
  stripped by the APNs size guard, or APNs rejecting the send), the
  ordinary input.needed banner is still delivered and the parked decision
  is marked `deliverable=false`, so the plugin immediately falls back to
  Hermes' native clarify path.
- Bounds: the plugin and relay accept at most 8 questions × 8 choices per
  batch (validation mirrored on both sides). That is the protocol/store
  ceiling, NOT a guarantee that every valid batch fits an answerable card:
  answerable-card capacity is bounded by Apple's ~4 KB APNs payload limit,
  and therefore by the actual byte size of the question and choice text. A
  valid batch whose serialized decision exceeds the payload budget is
  never truncated or split — Hermes is still waiting on every qid, so a
  partial card would collect answers for a batch that can never complete —
  instead the WHOLE structured decision is dropped from the push (plain
  input.needed banner, decision parked `deliverable=false`) and the plugin
  falls back to Hermes' native clarify path.
- The plugin returns the batch to Hermes exactly in the built-in tool's
  result shape (`{"responses": [...]}`, multi-select answers parsed back to
  lists). Protocol provenance comes from the original invocation: a
  one-entry `questions[]` call is batch protocol even though it has a
  single question, while legacy scalar calls keep the scalar result shape.
- Releasing a decision (native path won) is fired off the answer's critical
  path on a daemon thread, so a slow or unreachable relay can never delay
  the user's native answer.

Known limitation: the relay cannot see answers made natively (Hermes
Desktop/TUI answer through the gateway), and the gateway cannot see relay
answers. Whichever surface completes the WHOLE batch first wins the tool
call; the plugin then releases the parked decision (`DELETE
/v1/decisions/:id`) so late device answers are rejected rather than
reported as accepted. A batch answered partly natively and partly by relay
stays open until the gateway's configured clarify timeout bounds it.

### Decision retention limits

The relay allows up to 32 active unresolved decisions per installation and
1,024 across the relay (`RELAY_MAX_ACTIVE_DECISIONS`). A scalar decision is active until answered or cancelled;
a batch remains active until every question is answered or the decision is
cancelled. Completed and cancelled decisions preserve their answers and locks
for the two-hour decision TTL, within separate retained-record caps of 128 per
installation and 4,096 across the relay (`RELAY_MAX_RETAINED_DECISIONS`).
Active decisions count toward both retained caps. Since settled records stay
for two hours, those retained caps also impose a maximum admission throughput
of 64 records per installation per hour and 2,048 records per hour across the
relay, averaged over a full retention window. Decisions are stored in the data
file and a full batch record can reach about 13 KB, so the relay-wide caps
also bound how large a flood can make that file.

When any limit is full, a new clarify event receives HTTP `429`
`decision_capacity_exceeded`. The relay sends no push and does not consume the
event ID. The shipped plugin sends a `429` again a few times within about 30
seconds (the same event ID, like any other transient failure), then logs the
rejection and gives up; a caller may explicitly retry the same event ID
after capacity becomes available and before the event is accepted. Hermes'
native clarify path remains available, and existing decisions remain
answerable, pollable, and cancellable.

The global limits apply across every installation. Because installation
self-registration is open to anyone with a phone running the app, one actor
can create multiple installations,
occupy the global active and retained pools, and keep refreshing them as
records expire, denying new relay clarifies indefinitely. The relay cannot
identify a shared actor or enforce fairness from installation IDs alone.
Existing answers and Hermes' native clarify path remain available during
saturation; the global caps are a bounded storage tradeoff, not a tenant
fairness boundary.

## Gemini Live tokens

Conduit's Gemini Live voice mode talks to Google directly from the phone, but
the Gemini API key stays on your Hermes host. The plugin adds two routes to the
Hermes dashboard, behind the dashboard's normal auth:

| Method | Path | Returns |
|--------|------|---------|
| GET | `/api/plugins/conduit_push/gemini-live/status` | `{ok, available, reason?, model}` |
| POST | `/api/plugins/conduit_push/gemini-live/token` | `{ok, token, expires_at, new_session_expires_at, model, websocket_url}` |

Set the key in the profile's `.env` (the same key Hermes' Gemini TTS uses):

```bash
GEMINI_API_KEY=...        # or GOOGLE_API_KEY
CONDUIT_GEMINI_LIVE_MODEL=gemini-3.8-live   # optional override
CONDUIT_GEMINI_LIVE_API_VERSION=v1alpha      # optional; ephemeral tokens are v1alpha today
```

Each token is a Google ephemeral token: one use, locked to that model, valid
for 30 minutes, and it must open its Live session within a minute. Conduit
asks for a fresh token for every connection, capped at 20 per minute per
profile. The API key itself is never returned or logged. `?profile=<name>` resolves another profile's key, as the
`/api/audio/*` routes do, and fails with 503 rather than falling back to the
default profile if it can't. Restart the dashboard (`hermes gateway restart`)
after updating the plugin so the routes mount.

## Web search for voice lookups

Gemini Live can answer quick questions (weather, news, facts) with a web
search. Instead of Google Search, which Google meters separately, Conduit can
run those lookups on this host's own web search backend: whatever
`hermes tools` set up (SearXNG, Firecrawl, Tavily, and so on). Nothing extra
needs configuring on the plugin side.

| Method | Path | Returns |
|--------|------|---------|
| GET | `/api/plugins/conduit_push/web-search/status` | `{ok, available, reason?, backend?}` |
| POST | `/api/plugins/conduit_push/web-search` with `{query, limit?}` | `{ok, query, results: [{title, url, snippet}]}` |

Results are titles, URLs and snippets only (at most 5), and searches are capped
at 30 per minute per profile. A search that takes more than 20 seconds returns 504, and a backend's own error text stays in the Hermes log: Conduit only sees Hermes' configuration messages (such as "No web search provider configured") or a generic failure. `?profile=<name>` searches with that profile's
backend and keys, as the Gemini Live routes do.

## Memory for voice

Gemini Live can use what this host's Hermes remembers, whichever memory setup
it runs, without Conduit knowing the backend. Both routes are read-only in the
sense that the plugin issues no write calls: no `sync_turn`, no memory tool. A
provider may still keep its own caches or bookkeeping while it recalls. A
saved call is written to memory once it ends instead, as a chat is (see
[Call end](#call-end)).

| Method | Path | Returns |
|--------|------|---------|
| GET | `/api/plugins/conduit_push/memory/context` | `{ok, available, reason?, provider, recall, context}` |
| POST | `/api/plugins/conduit_push/memory/recall` with `{query}` | `{ok, available, results}` |

`context` is the built-in `MEMORY.md` / `USER.md` snapshot Hermes puts in its
own system prompt (respecting `memory.memory_enabled` and
`memory.user_profile_enabled`), capped at 8,000 characters. An external
provider named by `memory.provider` (Honcho, Mem0, Holographic, and so on) is
reached through recall instead: its own prompt block mostly describes tools the
voice model can't call. `provider` is
`"builtin"`, the external provider's name, or `null`. When there is nothing to
use, `available` is false with `reason` `"unsupported"` (a Hermes without the
memory modules) or `"disabled"` (memory off, or nothing stored yet).

`recall` is true only with an external provider: the built-in store is already
all in `context`. Then `POST /memory/recall` returns the provider's recall for
`query` (at most 500 characters) as `results`, capped at 4,000 characters.
Without a provider it returns `available: false` and empty `results`, not an
error. Recalls and context reads are each capped at 30 per minute per
profile, time out after 10 seconds (504), and a provider's own error text stays
in the Hermes log. The provider is started once per profile (as the
`conduit_voice` platform, session `conduit-voice`) and restarted when
`memory.provider` changes. Calls into it run one at a time. If one hangs, that
profile's memory routes answer 503 at once until it returns, and other profiles
are unaffected. `?profile=<name>`
reads that profile's memory, as the other routes do.

## Personality for voice

Conduit's voice modes can keep the agent's personality without reading stage
directions or emoji aloud.

| Method | Path | Returns |
|--------|------|---------|
| GET | `/api/plugins/conduit_push/personality` | `{ok, available, text}` |

`text` is the profile's `SOUL.md` as Hermes itself loads it (`load_soul_md`,
with its injection scan and legacy-protocol stripping), capped at 8,000
characters. `available` is false with empty `text` when there is no
`SOUL.md`, it is empty, or the Hermes is too old to have `load_soul_md`.
Reads are capped at 30 per minute per profile, unexpected errors are reported
by type only, and `?profile=<name>` reads that profile's `SOUL.md`.

When Conduit delegates a spoken turn to Hermes, the plugin's `pre_llm_call`
hook sees Hermes' voice-live note on that turn and adds one line to the user
message: keep the usual personality through word choice and tone, but write
only words meant to be spoken, with no stage directions or narrated actions,
no sound effects, no emoji, and no markdown. Other turns are left alone.

## Qwen Live tokens (plugin 0.75+)

Qwen Live is a separate provider beside Gemini Live in Conduit's phone, Watch
and CarPlay pickers. Both can be set up at once, and switching between them in
Conduit needs no environment change or gateway restart. Like Gemini Live,
call audio goes from the device straight to Alibaba Cloud (DashScope), and the
DashScope API key stays on your Hermes host.

Set these in the profile's `.env`:

```dotenv
DASHSCOPE_API_KEY=your-model-studio-key
CONDUIT_QWEN_LIVE_WORKSPACE=your-workspace-id
CONDUIT_QWEN_LIVE_REGION=cn-beijing          # or ap-southeast-1 (Singapore)
CONDUIT_QWEN_LIVE_MODEL=qwen3.8-omni-flash-realtime
CONDUIT_QWEN_LIVE_VOICE=Tina
```

The key and workspace must belong to the region. The model must be a Qwen
Realtime model, and the voice a plain voice name (letters, digits, `-`, `_`,
up to 64 characters); the host voice is only a default that Conduit's Qwen
settings can override without touching Gemini's voice.

| Method | Path | Returns |
|---|---|---|
| `GET` | `/api/plugins/conduit_push/qwen-live/status` | `{ok, provider: "qwen", available, model, reason?}`; `reason` is `no_api_key` without `DASHSCOPE_API_KEY`, or says which setting is wrong |
| `POST` | `/api/plugins/conduit_push/qwen-live/token` | `{ok, provider: "qwen", token, expires_at, model, websocket_url, voice}` |

Each token is a DashScope temporary API key (`POST /api/v1/tokens`, valid for
2 minutes) for one connection; Conduit sends it in the WebSocket upgrade
header, never in the URL. Both routes take `?profile=` like the Gemini ones,
answer `no-store`, and are rate limited per profile separately from Gemini. A
mint that fails is an error: it never falls back to Gemini or returns the
permanent key. There is no host-wide provider switch.

On the Watch, a grant asks for `qwen_live_token` instead of `live_token` (see
Apple Watch lookups and jobs). Saved Qwen calls use the `qwen-live` engine;
Gemini calls keep `gemini-live`. Conduit asks for consent to Alibaba Cloud
separately from Google, and Qwen calls use Hermes web search for lookups.

## GPT-Live on a ChatGPT subscription

This is the host half of [hermes-agent#108940](https://github.com/NousResearch/hermes-agent/pull/108940),
shipped in the plugin so Conduit doesn't wait on the upstream merge. It lets a
GPT-Live voice session bill your ChatGPT/Codex subscription instead of an OpenAI
API key. First sign in on the Hermes host with `hermes auth` and choose OpenAI Codex.

| Method | Path | Returns |
|--------|------|---------|
| GET | `/api/plugins/conduit_push/gpt-live/status` | `{ok, auth, available, reason, model, voice, source, api}` |
| POST | `/api/plugins/conduit_push/gpt-live/session` | `{ok, auth, session: {id}, transport: {type, sdp}, source, voice, briefing_applied, greeting_applied}` (plus `model` on the API path) |

The session route takes `{"sdp": "<WebRTC offer>", "history": [...], "voice": "cove", "briefing": "..."}` (history
optional, last 40 items kept; voice optional and, when given, used instead of `subscription_voice`; briefing optional, up to 65,536 characters (32,768 before 0.52.1), and added to the session instructions so the model has Conduit's rules before it speaks; greeting optional: absent, the model stays silent until the user speaks; `""` or text, it greets the user first, saying the text when given, up to 200 characters; call_opening optional (0.17.1+): `true` for a call Hermes placed, so the model speaks first with the opening Conduit's briefing gives (without a briefing it only says it's calling, never guessing why) and any greeting is ignored; `greeting_applied` is then true too, as it means the model was told to speak first) and returns the SDP answer plus the `voice` it used. The plugin posts the
offer to the Codex voice service with the host's Codex OAuth token and ChatGPT
account id; neither is ever returned to Conduit or logged (a failure in Hermes' own exchange logs only its exception type). Status only checks that a
sign-in exists; the account's voice entitlement is checked when a call starts.
A sign-in, quota or connection failure is an error: **it never falls back to
API billing**. Sessions are capped at 10 per minute per profile.

Optional settings in the profile's `config.yaml`, the same keys upstream uses:

```yaml
voice:
  gpt_live:
    subscription_model: gpt-live-1-codex
    subscription_voice: cove
    instructions: ""   # extra persona sentences
```

When the host's Hermes ships the same exchange, both routes hand the request to
Hermes (`source: "hermes"`) and keep serving the same URLs, so Conduit needs no
change. `voice.gpt_live.auth` is Hermes desktop's setting; these routes never
read it. A session request is billed to the subscription unless it names the
API key itself (below).

### GPT-Live on an OpenAI API key (plugin 0.22+)

Conduit can also start GPT-Live on the host's OpenAI API key, through the
public GPT-Live API (`POST /v1/live/sessions`, the same one Hermes desktop's
`voice_chat_mode: gpt-live` uses). OpenAI bills that per minute of session
time (see OpenAI's pricing for `gpt-live-1`); the ChatGPT subscription is not
used. Background jobs still run on Hermes' own model, as they do on the
subscription.

The two paths are kept apart so nobody is billed for the one they didn't
choose:

- **Conduit has to ask for it by name.** The session route takes
  `"billing": "api"`; a request without `billing` (every Conduit before this
  feature) or with `"billing": "subscription"` is a subscription call. Any
  other value is a 400.
- **The profile has to allow it.** `voice.gpt_live.conduit_api_billing: true`
  turns it on; anything else (missing, `false`, `"true"`, `1`) keeps it off
  and the request is a 403. An OpenAI key that is set for speech-to-text or
  TTS is not enough on its own.
- **No fallback, either way.** A failed subscription call never becomes an
  API call, and a failed API call never becomes a subscription call. Every
  API-path error says the subscription was not used.
- **`auth` says which path ran.** The session answer carries `"auth": "api"`
  or `"auth": "subscription"`. Conduit drops the call before any audio when it
  doesn't match what it asked for.
- **A daily cap.** A profile may start `voice.gpt_live.conduit_api_daily_calls`
  API-billed calls per day (default 20; `0` turns API calls off, a 403). A
  call OpenAI refuses or never answers (a rejected key, an outage) gives its
  slot back; once OpenAI has said yes, the slot stays counted even if its
  answer turns out unusable or is cut off mid-read, since a billed session may have started.
  The count is kept in memory, per dashboard process: a restart starts it
  again, and a dashboard run with several worker processes allows the cap in
  each.
- **What the host can't cap.** The cap counts calls started, not minutes.
  Once a call is set up, its audio runs between Conduit and OpenAI without
  passing through this host, so the host can't end a call or see how many are
  open. Call length is limited by Conduit (a per-call time limit, 20 minutes
  by default), which is a client-side control: set a spending limit on the
  OpenAI key itself if you need a hard ceiling.

```yaml
voice:
  gpt_live:
    conduit_api_billing: true      # allow API-billed calls from Conduit
    conduit_api_daily_calls: 20    # per profile per day
    model: gpt-live-1              # the API path's model and voice
    voice: marin                   # marin, cedar, quartz, ripple, vesper, willow, ...
    # api_key: sk-...              # else VOICE_TOOLS_OPENAI_KEY / OPENAI_API_KEY
    # base_url: https://api.openai.com/v1
```

The key is resolved the way Hermes desktop resolves it
(`voice.gpt_live.api_key`, then `VOICE_TOOLS_OPENAI_KEY`, then
`OPENAI_API_KEY`) and is never returned to Conduit or logged. `base_url` must
be an https URL with a host and no query or fragment. The shared keys only
ever go to `api.openai.com` on the standard port: a `base_url` on any other host needs its own
`voice.gpt_live.api_key`. Only OpenAI's GPT-Live contract is supported; other
services behind `base_url` (Azure Foundry, say) are untested and may expect a
different auth header.

OpenAI's answer doesn't echo the session, so on the API path
`briefing_applied`, `greeting_applied` and `voice` say what the plugin sent,
not what OpenAI confirmed. A 401 from OpenAI is reported as a rejected key and
a 429 is passed on as a 429; neither carries OpenAI's response body. The status route reports the API path under its own key, leaving
the top-level fields the subscription's:

```json
{"ok": true, "auth": "subscription", "available": true, "...": "...",
 "api": {"auth": "api", "enabled": true, "available": true, "reason": null,
         "model": "gpt-live-1", "voice": "marin", "daily_limit": 20, "host": "api.openai.com"}}
```

The Apple Watch's GPT-Live bridge always uses the subscription.

## Grok Live on a SuperGrok subscription

Grok Live is xAI's realtime voice model. Conduit reaches it through a socket on
this plugin: the plugin opens `wss://api.x.ai/v1/realtime` with the host's xAI
credential and relays frames both ways without reading or changing them. The
credential is the host's SuperGrok sign-in (`hermes auth add xai-oauth`) or, without one, `XAI_API_KEY`. That's the same order Hermes uses for its
other xAI endpoints, and neither credential is ever sent to Conduit or logged.

| Method | Path | Returns |
|--------|------|---------|
| GET | `/api/plugins/conduit_push/grok-live/status` | `{ok, available, reason, auth, model, voice, transport}` |
| WebSocket | `/api/plugins/conduit_push/grok-live/socket` | xAI realtime events, relayed |

The socket takes the dashboard's own WebSocket credential (`?ticket=`, as
`/api/audio/speak-stream` does) and an optional `?profile=`. `auth` in the status
says which credential would be used: `subscription` or `api_key`. When the
plugin or xAI refuses a call, the socket closes with a reason Conduit shows:

| Code | Meaning |
|------|---------|
| 4401 | Dashboard auth failed (past 30 refusals a minute on the host, refused upgrades get a bare HTTP 403 instead) |
| 4503 | No xAI credential on the host, or its SuperGrok sign-in couldn't be read |
| 4400 | xAI refused the credential or the call, or closed it with one of its own 3000-4999 codes (named in the reason) |
| 4429 | Too many connections (10 per minute or 3 open per profile, 8 open on the host), or xAI rate limiting |
| 4502 | xAI unreachable, the connection to xAI dropped, or the host timed out reading its xAI sign-in (retryable) |
| 4500 | Grok Live failed on the host, e.g. it can't scope the profile (see the dashboard log) |

Frames from Conduit over 256 KiB close the socket with 1009. The dashboard's
server reads a whole frame before the plugin sees it, so its own WebSocket size
limit (uvicorn's `ws_max_size`, 16 MiB by default) is what bounds memory per
frame; only a signed-in dashboard client can send one.

Optional settings in the profile's `config.yaml`:

```yaml
voice:
  grok_live:
    model: grok-voice-latest   # or CONDUIT_GROK_LIVE_MODEL in .env
    voice: eve
```

## Your own speech server (plugin 0.39+)

Conduit's Classic voice can listen through a speech server you run yourself,
so your voice is recognized on your own machines. Any server that speaks the
OpenAI Realtime WebSocket protocol works, for example Hugging Face
[speech-to-speech](https://github.com/huggingface/speech-to-speech)
(`speech-to-speech serve`) or the one Hermes Live Voice's local mode starts.
It can run on another machine (an LXC, a GPU box, a Mac on your network).

Conduit never connects to it directly. This plugin relays Conduit's realtime
events to the server and back, adding only `Authorization: Bearer <api_key>`
when a key is set. The phone and Watch keep talking to Hermes alone, so the
speech server never needs to be exposed outside your network.

Set it per profile in `config.yaml` (Conduit's Voice settings write the same keys):

```yaml
voice:
  speech_server:
    url: ws://192.168.1.20:8765/v1/realtime   # http(s) works too; a bare host gets /v1/realtime
    api_key: ""                               # optional, for a server behind an auth proxy
    api_key_file: ""                          # optional: read the key from this file instead
```

`api_key_file` names a file holding the key (an absolute path, or one starting
with `~`; at most 16 KiB of UTF-8 text), read again on every connection, so a
key that rotates on disk (a Kubernetes projected service-account token, a
secret a sidecar refreshes) is always current. `api_key` wins when both are
set. A file that's set but can't be used (missing, empty, not a regular file)
is an error, never "no key"; the reason goes to the Hermes log.

Plain `ws://` is only allowed for loopback, private, link-local and Tailscale
addresses or LAN-style names (`localhost`, single-label names, `.local`, `.lan`,
`.internal`, `.home.arpa`, `.ts.net`). A public host needs `wss://`. Names are
judged by their shape and not resolved, so this is a guard against mistakes,
not a security boundary.
speech-to-speech has no authentication of its own, so keep it on your network.

| Method | Path | Returns |
|--------|------|---------|
| GET | `/api/plugins/conduit_push/speech-server/status` | `{ok, configured, available, url, has_key, reason}` |
| POST | `/api/plugins/conduit_push/speech-server/test` | `{ok, connected, url, latency_ms, event, reason}`: dials the server from the host and waits for its first event |
| WebSocket | `/api/plugins/conduit_push/speech-server/socket` | OpenAI Realtime events, relayed |

The socket takes the dashboard's WebSocket credential (`?ticket=`) and an
optional `?profile=`. Close codes: 4503 not configured or invalid URL, 4401
dashboard sign-in required, 4429 rate limited, 4400 the server refused, 4502
unreachable or dropped (retryable), 4500 the relay failed on the host, 1009 a
frame over 256 KB. A socket that sends nothing for 60 seconds is closed.

## Local Live: live calls through your own voice server (plugin 0.42+)

The same speech server can run whole live calls. speech-to-speech listens,
decides when you've finished, and speaks the reply, asking an
OpenAI-compatible "model address" for the words. Point that model address at
this plugin and Hermes answers each turn in the chat the call belongs to, with
that chat's profile, tools and memory. The call's audio goes through the
relay above.

1. Set the speech server above (Conduit's Voice settings do it).
2. In Conduit's Local Live settings, copy the model address and key, or read
   them from `GET /local-live/status`. The model address is your dashboard's
   address plus `/api/plugins/conduit_push/local-live/v1`, so the voice
   server has to reach the dashboard the way your phone does (LAN, Tailscale
   or a public domain).
3. Start speech-to-speech with the chat-completions backend:

```bash
speech-to-speech serve --llm_backend chat-completions \
  --responses_api_base_url https://<your dashboard>/api/plugins/conduit_push/local-live/v1 \
  --responses_api_api_key <key from Conduit> \
  --model_name conduit --stream_batch_sentences 1
```

The model routes take that key as a bearer token instead of the dashboard
sign-in, through Hermes' token-route seam
(`hermes_cli.dashboard_auth.token_auth`). The key works on those two routes
only. A Hermes without the seam reports Local Live unavailable, and so does a
public dashboard with no other sign-in provider. A key is per profile and
names its profile. Rotating it stops the old one at once in the dashboard's process (other processes accept it for up to 30 seconds, until their cache expires).

The model name is ignored, and only the newest user message is used, since
Hermes keeps the history. One live call per profile at a time: a turn goes to
the profile's newest Local Live socket. With no call open, the routes answer a
fixed line, so the server's warmup request succeeds.

| Method | Path | Returns |
|--------|------|---------|
| GET | `/api/plugins/conduit_push/local-live/status` | `{ok, available, reason, model_path, model_name, api_key, speech_server, in_call, last_request_at}` |
| POST | `/api/plugins/conduit_push/local-live/key` | `{ok, api_key}`: a new key |
| WebSocket | `/api/plugins/conduit_push/local-live/socket` | the speech server relay, plus turn frames |
| POST | `/api/plugins/conduit_push/local-live/v1/chat/completions` | OpenAI chat completions (streaming or not), key auth |
| GET | `/api/plugins/conduit_push/local-live/v1/models` | `{object: "list", data: [{id: "conduit"}]}`, key auth |

Turn frames on the socket, beside the relayed realtime events:

- plugin to Conduit: `{"type": "conduit.turn", "turn_id", "text"}`, and
  `{"type": "conduit.turn.cancel", "turn_id"}` when the server stops listening
  (you talked over the reply) or a newer turn starts.
- Conduit to plugin, never forwarded: `{"type": "conduit.turn.delta",
  "turn_id", "text"}`, then `conduit.turn.done` or `conduit.turn.error`.
  A turn with no frame for 90 seconds ends.

`/local-live/socket?mode=speak` is Classic voice's reply voice: Conduit sends
each reply sentence as a user `conversation.item.create` plus
`response.create`, and the model route reads that text back unchanged, so
the server speaks Classic's replies in its own voice. Classic then uses one
server for listening and speaking. While the reply plays, Classic may open
its listening socket too, so start the server with `--num_pipelines 2`.

### Local Live on a read-back speech server (plugin 0.50+)

Some speech servers only listen and read text aloud, with no model of their
own: a response reads back the user text it's given, and a session that asks
for automatic responses (`turn_detection.create_response: true`) is refused.
The farm's fleet voice adapter is one. Such a server never asks the model
address for a reply, so the plugin runs the call's turns on the relay instead
(capability `local-live-read-back`), on the phone's socket and the Watch's
bridge alike:

- When the server refuses automatic responses for that reason
  (`realtime_live_unavailable`, or an error naming
  `turn_detection.create_response`), the plugin sends the call's
  session.update again with `create_response` and `interrupt_response` off.
  Conduit never sees the refusal.
- Each finished transcription (`conversation.item.input_audio_transcription.completed`)
  starts the call's turn, as the model address would. The readiness check's
  marker and a typed turn do too, so neither is read aloud.
- The reply, as it streams, goes back in sentences, one read-back
  `response.create` (inline input) per sentence, one at a time. The first
  sentence goes as soon as it ends; later ones join until they're 40
  characters, and text that waits 1.2 s goes anyway.
- Conduit sees one response per reply, however many sentences it takes, with
  its transcript spaced between them, and an empty reply as a response that
  begins and ends.
- When the user starts speaking over the reply (`input_audio_buffer.speech_started`),
  the reply stops: the plugin cancels the server's response, drops its late
  audio, and lets the turn go, as a server that answers by itself would.
  While nothing of the reply has been said yet (Hermes still at work), only
  words stop it: its first sentence waits until the speech is transcribed
  (10 s at most), the words start a new turn, and a sound that transcribes
  to nothing lets the reply go on.
- A cancelled response the server says nothing more about is over after 2 s,
  and one that goes quiet (not started, or no audio) for 30 s is given up on.
- A server whose transcripts of two sentences in a row don't match them
  (sentences of five words or more, without digits) isn't reading back. The
  call ends with a reason: close code 4400 on the phone's socket, an `error`
  with code `refused` on the Watch's bridge.

Nothing changes for a server that answers by itself.

### Agent mode: a fast model in front of Hermes (plugin 0.49+)

One Hermes turn per utterance is slow to start talking. In agent mode a fast
model of your choosing holds the conversation and hands real work to Hermes
as background jobs, the way GPT-Live, Gemini and Grok calls do. Conduit's
Live > Local mode turns it on; Classic's "Hands-free on your speech server"
keeps one Hermes turn per utterance.

Set the fast model in the profile's config (Conduit's Local settings write
it). Any OpenAI-compatible chat-completions server works, local (llama.cpp,
vLLM, Ollama, LM Studio) or remote. Plain `http://` only reaches private,
Tailscale and LAN addresses, since the key goes with every request.

```yaml
voice:
  local_live:
    front_model:
      base_url: http://10.1.10.52:8080/v1   # /chat/completions is added
      model: qwen3-4b-instruct
      api_key: sk-...                        # optional; or api_key_file
```

How a call runs:

- On connecting, Conduit sends `{"type": "conduit.agent", "instructions",
  "tools"}` (the call's live-model instructions and its tools: start_job,
  list_jobs, cancel_job and the rest). The plugin answers
  `{"type": "conduit.agent.ready", "tools": n}`.
- Each utterance the speech server asks about goes to the fast model with
  the call's own conversation (the server's history and system prompt are
  not used) and streams back as it arrives. `<think>` blocks are dropped.
  The speech server never sees a tool.
- A tool call goes to Conduit as `{"type": "conduit.tool.call", "call_id",
  "name", "arguments"}`; Conduit runs it and answers `{"type":
  "conduit.tool.result", "call_id", "output", "speak"}`. start_job answers
  as soon as the job runs, in the call's chat with its job card. A call that
  waits more than 30 seconds tells the model it is still running; a result
  that comes after that, with `speak` true, is handed back as
  `conduit.tool.late` and Conduit says it as an update.
- A job's outcome comes back the way Grok Live's does: once the call is
  quiet, Conduit sends it to the speech server as a user item plus
  `response.create`, and the fast model tells you what Hermes found.
- `{"type": "conduit.agent.note", "text"}` adds context the model keeps
  without answering (a message typed in the call's chat).
- The readiness check is still answered by Conduit. With no fast model set,
  or with one that fails before saying anything, the utterance goes to
  Hermes as a turn instead.

An engine that runs its own fast model (Pipecat behind the relay, say) uses
the same tools with the Local Live key:

| Method | Path | Returns |
|--------|------|---------|
| GET | `/api/plugins/conduit_push/local-live/v1/tools` | `{in_call, instructions, tools}`: the call's tools, chat-completions shape |
| POST | `/api/plugins/conduit_push/local-live/v1/tools/call` | `{name, arguments}` in, `{output}` out; 409 with no call |

Job outcomes reach such an engine on the relay as a user
`conversation.item.create` plus `response.create`. `GET /local-live/status`
adds `front_model: {configured, available, base_url, model, has_key,
reason}`, and the capability is `local-live-agent`.

### Local Live on the Apple Watch (plugin 0.48+)

A Watch call on Local Live uses the same server, key and model address. The
Watch runs the phone's Local Live session through the call grant's audio
bridge (engine `local_live`, capability `watch-local-live`), and this host
relays its audio and events to the speech server. When the server asks the
model address for a reply, the turn goes to the Watch's call, and this host
runs it as a spoken turn in a chat of the call's own ("Watch call"), since a
Watch can't run a Hermes turn. Only the readiness check's ask goes to the
Watch, which answers it with its greeting. A Watch that rejoins on the same
grant sends `resume_session_id` (from `started.session_id`) and carries on in
that chat. The grant's end closes the chat; it stays in Hermes' history.
`GET /watch-audio/status?profile=` reports `engines.local_live`, and a grant
lists `local_live` once Local Live is available for the profile and the
dashboard serves Hermes' chats.

A Watch whose start says `"approvals": true` answers Hermes' approval
requests on its own card (plugin 0.50.1+, capability
`watch-local-live-approvals`). Any other Watch is told to answer in the chat,
as before.

- Host to Watch, as events on the call's stream:
  - `{"type": "conduit.approval", "id", "command", "description", "choices", "pending", "cut"}`
    shows the chat's oldest pending request. Hermes answers its oldest
    first, so requests come one at a time.
  - `conduit.approval.gone` (with its `id`) takes the card away. That happens
    when Hermes withdraws the request (`approval.cancelled`, or a
    `request.cancel` naming it when Hermes asked by server request), the
    turn ends, or Hermes' own approval timeout (`approvals.timeout`) passes.
  - `conduit.approval.done` (`id`, `resolved`) answers the Watch's answer.
- Watch to host: `{"type": "conduit.approval.answer", "id", "choice"}`, where
  `choice` is one the card offered. It goes to Hermes as `approval.respond`.
- `choices` is `["once", "deny"]` only while exactly one request is pending
  and its command is shown whole (up to 2,000 characters). Otherwise it is
  `["deny"]`, because Hermes' queue can drop a request the plugin never
  hears of (one answered in Conduit), so with more pending the card might
  not be Hermes' oldest.
- The call says "Hermes needs your approval. It's on your Watch." once a
  reply, and the turn stays open: what Hermes says after the answer is
  spoken too. While the card waits, the plugin keeps the turn alive (an SSE
  comment on the model address every 20 s), up to Hermes' own timeout. A
  server that asks the model address without streaming can't be kept so,
  and a turn never lasts past 10 minutes.
- One answer at a time: a second tap, or a rejoined Watch's answer, isn't
  taken, and the next card waits, until Hermes has the first.
- The requests are kept by the call's chat, so a Watch that rejoins on the
  same grant is shown the one still pending, even one Hermes asked between
  the two streams.

## Voice call transcripts

Gemini Live, GPT-Live and Grok Live calls never run a Hermes turn, so nothing records
them. These routes let Conduit save a call's transcript as an ordinary session
in this profile's history, written straight into the session store. They never
start the agent or call a model. A resumed call appends to the same session.

| Method | Route | Returns |
| --- | --- | --- |
| POST | `/api/plugins/conduit_push/voice/sessions` with `{call_id, engine, session_id?, chat_id?, title?, turns: [{index, role, text, at?}]}` | `{ok, session_id, chat_id?, written, appended, created}` |
| GET | `/api/plugins/conduit_push/voice/chat-calls?chat_id=[&call_id=]` | `{ok, chat_id, calls: [{call_id, engine, turns?: [{role, text, at}]}]}` |
| GET | `/api/plugins/conduit_push/voice/tags` | `{ok, tags: {session_id: {kind, engine?, archived?, parent_id?, parent_title?}}}` |
| POST | `/api/plugins/conduit_push/voice/tags` with `{session_id, kind: classic or job, parent_id?, parent_title?}` | `{ok, session_id, kind, ...}` |
| POST | `/api/plugins/conduit_push/voice/archive` with `{session_id}` | `{ok, session_id, archived}` |
| GET | `/api/plugins/conduit_push/voice/summary?session_id=` | `{ok, available, text, covers}` |
| POST | `/api/plugins/conduit_push/voice/summary` with `{session_id, text, covers}` | `{ok, session_id, covers}` |

- Saved sessions keep source `desktop` and no model. Hermes uses a session's
  source as the agent platform and restores the stored model on resume, so
  typing into a saved call behaves exactly like any Conduit chat.
- Each turn's `index` counts from 0 within its `call_id` (at most 100,000),
  with no gaps, and a turn is sent only once it's final: it must have text,
  and an index never changes content afterwards. A row is append-only, so
  the host keeps each call's highest written index (for a session's last 500
  calls): indices at or below it are replays of a retried save and are
  reported as `skipped`, and new turns must continue right after it. A gap
  gets 400 instead of being stored out of order or lost. `written` is one
  past that highest index, where the next save starts. One caveat: the
  append and that record are two separate writes, so a failure between them
  can repeat a line on retry (it never loses one).
- A call attached to a chat (started in it, or a Hermes call from it) sends
  `chat_id`, the chat's stored session id, and no `session_id`. Its turns are
  kept with that chat in `state_meta` instead of a session of its own, under
  the same index rules, and the chat's own messages are never touched;
  `session_id` comes back null and `created` false (no session is made).
  `voice/chat-calls` lists them for the chat; turns come only for the one
  call named by `call_id`.
  A chat keeps its latest 50 calls of at most 2000 turns each.
  When the call ends it is written to the memory provider under the chat's
  session, but no session-end hooks fire: the chat goes on. A chat that no
  longer exists gets 422, and Conduit then saves the call on its own. Older
  plugins ignore `chat_id` and save a session as before.
- A call saved as its own session (one not kept with a chat) is archived
  when its first save lands, with Hermes' auto-archive stamp
  (`auto_archived`), so Hermes Desktop's chat list doesn't fill up with
  calls. Typing into the row reopens it, and Hermes then un-archives it by
  itself, so a call continued as a chat comes back. A resumed call doesn't
  touch the flag, a retried first save finishes an archive its first
  attempt didn't get to (the call's tag records a done one with
  `archived: true`), and an archive the user sets or clears stays. Conduit keeps
  listing these rows with its calls, and Desktop's Archived view and search
  still find them. Hermes builds without the stamp leave the row listed.
- `voice/archive` archives a Classic voice chat (tagged `classic`) the same
  way when the call that started it ends; Conduit sends it. A pinned or
  already archived chat is left as it is (`archived: false`), and any other
  session gets 422. Such a chat may still be open when its call ends, so
  nothing reopens it: the next turn run in a voice chat archived this way
  (a call row or a Classic chat) un-archives it from the `pre_llm_call`
  hook, the way Hermes' reopen does.
- A create is idempotent per `call_id`: a retried first save whose response
  was lost continues the row it made instead of starting a second one.
- Appends and summaries go only to rows saved as voice calls; any other
  session id gets 422. A gone row's call record and summary are cleared.
- Conduit's labels (voice call, classic voice chat, voice job) and the resume
  summaries live in Hermes' `state_meta` table, which the agent never reads.
- The routes return 501 on a Hermes without a session store, 409 while the
  session is being compacted, and 422 when the session was deleted or compaction
  has closed it (Conduit then saves the call as a new session). Writes are
  capped at 120 a minute and reads at 600 for the whole dashboard (429 past
  that). Writes assume one dashboard process, as Hermes runs it. Each
  request gets 20 seconds; a store call that hangs past that keeps its worker
  busy until Hermes' own SQLite timeout releases it, and may still complete, so
  a 504 doesn't mean the write didn't land (a retried save skips what did). Writes are serialized per profile, so a slow
  store in one profile doesn't hold up another. A 404 only ever means
  the plugin is too old to have the route.

### Call end

A live call runs no Hermes turn, so on its own Hermes never learns that it is
over. Conduit saves a call only once it has ended (hung up, dropped or timed
out), so a save that stores new turns ends that call's session, the same way
Hermes ends a Desktop chat:

1. The `on_session_end` hook fires with `session_id`, `completed: true`,
   `interrupted: false`, `model` (the voice engine, such as `gemini-live`),
   `platform: "desktop"` and `reason: "voice_call_ended"`.
2. The memory provider named by `memory.provider` (Honcho, Mem0, …) gets the
   call: it is started for that session (with the session's title, as Hermes
   does), sees each exchange through `sync_turn`, then `on_session_end` with the
   call's turns, and is shut down. A turn missing either side (a greeting
   before you spoke, a question the call ended on) isn't synced, as in Hermes.
3. The `on_session_finalize` hook fires with `session_id`, `platform` and
   `reason`, through Hermes' own `finalize_session`.

Only the call's new turns go to memory: a resumed call's earlier turns went
there when their own call ended. A retried save that stores nothing new ends
nothing. This runs in the background, one call at a time, so a slow provider
never holds up a save. A step that fails is logged and the rest still run. A
call whose end takes more than two minutes is left to finish on its own while
the next call ends; once three are stuck like that in a profile, that profile's
calls end without these steps until one finishes, and past 20 calls waiting a
call ends without them too (the log says so each time). A call that isn't saved
(saving turned off in Conduit, or no speech from you) ends nothing.

## Chat takeover

Hermes lets one app own a chat at a time. Hermes Desktop claims a chat on its
first turn and keeps the claim until the chat is closed there, so sending to
the same chat from Conduit is refused with "This chat is open in another Hermes
window/terminal". This route is what Conduit's **Take over** does: it drops the
other app's claim in `runtime/active_sessions.json`, under Hermes' own registry
lock, and Conduit's next send claims the chat.

| Method | Route | Returns |
| --- | --- | --- |
| POST | `/api/plugins/conduit_push/sessions/takeover` with `{session_ids: [..]}` | `{ok, status, surface?}` |

- `status` is `taken_over` (send again), `free` (nobody else holds it),
  `busy` (the other app is running a turn on it, it's a live worker or
  gateway whose turns can't be seen, or the gateway's ledger holds work on
  it; ask again shortly, nothing
  is interrupted; a marker that can't be read counts as running, and only a marker whose writer is provably dead, or a gone owner's marker that names no writer, is ignored) or
  `same_host` (this dashboard process still holds it, for example Conduit on
  another device; that claim is left alone).
- Hermes' gateway can also hold a chat with no claim at all: work it queued
  in its durable ledger (`session_admissions` and `worker_executions` in the
  profile's state.db), such as another bot's message (message_agent) running
  in a bot's Bot Chat. Hermes refuses every other send to that chat until the
  work ends, so the route answers `busy` without dropping anything, with
  `surface` `bot` for another bot's message and `gateway` for other work.
  Conduit says the bot is answering another bot and sends once it's done
  (plugin 0.77.6+). A ledger that can't be read is skipped (logged as a
  warning) and the claim rules apply.
- The other app writes its turn marker without the registry lock, so a turn
  it starts in the instant between the check and the takeover still loses
  its claim. That turn keeps running there; nothing is interrupted.
- Only Desktop and TUI claims are dropped while their owner is alive: they are
  the surfaces that write turn markers. A live claim from a CLI or kanban
  worker, a gateway, or with no surface answers `busy`, since a running turn there can't be
  seen. A claim whose owner process is gone is dropped whatever its surface,
  as long as no live claim of that kind also holds the chat.
- Only another process's claim is dropped; a claim without a valid pid is
  kept, since it can't be attributed. The other app isn't told: if you
  go back and send from Hermes Desktop, it still believes it owns the chat
  and doesn't see what you sent from Conduit until you reopen the chat there.
- `session_ids` takes 1 to 4 ids of one chat (its stored id and its live id),
  never ids of different chats.
  400 for a bad body, 501 on a Hermes without the ownership registry or turn markers, 503
  when the registry can't be read (ownership is never guessed), 429 past 60
  requests a minute, 504 if the takeover doesn't finish within 20 seconds (ask again: nothing is written after that, short of a write already under way).

The other way round (plugin 0.58.1+): a chat Conduit opened in the dashboard
holds its claim for as long as Conduit stays connected, which used to refuse
the same chat in Hermes Desktop's own process long after Conduit's turn ended.
The dashboard now lets go of Conduit's claim once the chat has been idle for a
minute: no turn running, nothing waiting on an answer, no delegated work, wake-up
or background process still to report, and no turn marker. The chat stays open
in Conduit, and its next send claims it again, or meets the usual refusal and
**Take over** if Desktop took it meanwhile. Only chats whose every client here
is Conduit count; a chat Desktop or the web dashboard also has open in this
process, and Bot Chats, keep their claim.

## Chats opened on Desktop (plugin 0.12+)

Conduit's Unread filter can count a chat as read when you have it open in
Hermes Desktop or the web dashboard on the same host. Hermes has no hook for
a client opening a chat, so the plugin wraps the gateway's `session.activate`
and `session.resume` handlers in the dashboard process. Per chat it keeps the
newest open (`opened_at`) and how long that chat then stayed the one its
Desktop connection had selected (`seen_through`), in
`<hermes home>/conduit-desktop-views.json` (at most 2,000 chats). It never
writes Hermes' own read flag, so Desktop's unread dots don't change.

| Method | Route | Returns |
| --- | --- | --- |
| GET | `/api/plugins/conduit_push/sessions/desktop-views?profile=&since=` | `{ok, observing, reason, views: {stored id: {opened_at, seen_through, client, open?}}}` |

- An open counts when it comes over the gateway's WebSocket from a browser
  engine (Desktop's renderer or the web dashboard: a `Mozilla/` User-Agent);
  `client` is `desktop` when it also says `Electron/`, else `browser`.
  Conduit's own socket, the TUI and hosted rooms never count. The
  User-Agent is the client's own claim, so this is best-effort sorting
  among clients that already hold a dashboard login.
- A connection's selection moves with its next open and ends when its socket
  closes (noticed within 5 seconds). `open: true` marks a chat that is
  selected right now; its `seen_through` is the moment of the read. The host
  can't tell whether anyone is looking at the window, and Desktop also
  re-attaches chats after a reconnect.
- `observing` is false, with a `reason`, while the gateway hasn't loaded
  (`gateway-not-in-process`, also what `plugins.isolation: host` reports) or
  when a Hermes update moved those handlers or the socket's headers
  (`gateway-unsupported`). The
  wrapper never fails a call and always returns Hermes' own response.
- `since` returns only chats seen after that time (seconds since the epoch);
  a value that isn't a finite number returns every chat. 429 past 60 reads a
  minute per profile.
- An open is skipped when the gateway can't say which profile its chat
  belongs to, rather than guessed onto the default profile.

## Quiet while you read in Desktop (plugin 0.20+)

A reply that comes back within 2 minutes in a chat Hermes Desktop (or the
web dashboard) has selected on this host sends no "response ready" push:
you most likely watched it arrive there. A longer turn still pushes, since
you have likely walked away, and every other notification stays as it was:
other chats, background jobs, approvals, questions, failed turns and calls.
There is nothing to set up.

- The selection is the one [Chats opened on Desktop](#chats-opened-on-desktop-plugin-012)
  follows: each Desktop connection's selected chat, ended when it switches
  chats or its socket closes. The host can't see whether the window is in
  front, so a short reply in a chat left selected on an unattended Mac stays
  quiet on the phone (Desktop's own notification still shows on the Mac
  when its window is in the background).
- The dashboard publishes the selected stored chat ids, per Hermes home, in
  `<hermes home>/conduit-desktop-selected.json` (private to the user) when
  they change, and renews the file every 30 seconds while anything is
  selected. The turn hooks trust it for 45 seconds, so a stopped dashboard
  stops quieting anything.
- A turn's 2 minutes run from its `pre_llm_call` to its `post_llm_call`. A
  turn with no start time, a file that can't be read, or `plugins.isolation:
  host` (where Desktop views aren't observed) all push as before.

## Quiet while you read in Conduit (plugin 0.60+)

A reply that lands in a chat you have open in Conduit sends no "response
ready" push, on whichever device you have it open: chatting on a Mac or an
iPad no longer buzzes the paired iPhone and the Apple Watch for each answer
you just watched arrive. Conduit knows when it is in front, so a long turn
stays quiet as long as you keep using Conduit; one left running with nobody
touching or typing for 2 minutes pushes, like Desktop's rule. Every other
notification stays as it was: other chats, background jobs, approvals,
questions, failed turns and calls. There is nothing to set up.

- While a turn runs in the open chat, with Conduit's window active and the
  chat not covered by the list, Conduit holds the chat as on screen with
  `PUT /api/plugins/conduit_push/notifications/on-screen?profile=…` and
  `{"device": "mac", "session_ids": ["<runtime id>", "<stored id>"],
  "hold_s": 90}`, renewing it every 30 seconds while the turn runs and
  someone used Conduit in the last 2 minutes, and sending `"hold_s": 0` once
  nobody has. Low Power Mode and Low Data Mode stretch the renewal like
  Conduit's other polls, and the hold stays three renewals long. Nothing is
  sent while no turn runs.
- After the turn ends the last hold runs out on its own, so the reply's own
  hooks still see it. Leaving the chat or the app sends `"hold_s": 0`, and
  the next reply there pushes as before.
- Each device kind (`iphone`, `ipad`, `mac`, `vision`, `watch`) keeps its own
  file in `<hermes home>/conduit-on-screen/` (private to the user), so one
  device leaving never ends another's hold (two devices of the same kind
  share a file, and the later report wins). A turn longer than 30 minutes
  pushes whatever Conduit says. A hold longer than 10 minutes, a
  file that can't be read, or a turn whose hooks don't share the profile's
  home with the dashboard pushes as before. Capability `chat-on-screen`.

## Reply notifications by source (plugin 0.35+)

If you also talk to Hermes in Desktop, a messaging app or the terminal, you
already see those replies there. In Conduit's Settings > Notifications you
choose, per profile, which sources send a "response ready" push: Conduit,
Hermes Desktop and the web dashboard, messaging apps (Telegram, Discord,
Slack, plugin platforms…), and the terminal. Everything is on until you turn
it off. Approvals, questions, failed turns, background tasks, cron runs (they
have their own switch) and calls don't follow this.

- The source is where the message that started the reply's turn was sent,
  not where the chat began: a Telegram chat you continue in Conduit is a
  Conduit reply. Hermes binds each turn to the connection that submitted
  last before it started, so a message queued meanwhile belongs to the
  next turn, not to the reply already running.
- Conduit's socket carries `X-Conduit-Client` (Watch jobs count as Conduit);
  another browser-engine socket is Desktop or the web dashboard. A turn
  with no app connection is placed by its Hermes platform: `cli` is the
  terminal, a Hermes messaging platform is a messaging app.
- A turn that can't be placed (an app connection run under `plugins.isolation:
  host`, the stdio TUI, a Hermes that moved these) pushes as before.
- The choice lives in the profile's `conduit-notifications.json`.
  `GET /api/plugins/conduit_push/notifications/reply-sources` reads it
  (`{"ok": true, "sources": {"conduit": true, "desktop": false, …}}`) and
  `PUT` with `{"source": "desktop", "notify": false}` changes one.
  Capability `reply-sources`.

## Where in Conduit a message was sent (plugin 0.51+)

Hermes knows a turn came from Conduit, not whether you typed it on an iPad
or said it to CarPlay while driving. Just before Conduit sends a message it
tells the plugin the surface and how the message was entered, and that
message's turn gets one line of context, for example
`[Conduit: this message came from CarPlay in the user's car, spoken. They are likely driving: …]`.
A message typed on an iPhone, iPad, Mac or Vision Pro adds that several
details only the user can give go in one clarify call with open questions,
which Conduit shows as a form they may leave partly blank (0.82+).

- `POST /api/plugins/conduit_push/turn-surface` with
  `{"session_id": "<runtime session id>", "text_sha256": "<hex>", "surface": "carplay", "input": "spoken"}`
  answers `{"ok": true, "recorded": true}`. `text_sha256` is the SHA-256 of
  the UTF-8 text Conduit sends in `prompt.submit`. Surfaces: `iphone`,
  `ipad`, `mac`, `vision`, `watch`, `carplay`, `siri`; `input` is `typed`
  or `spoken`, or left out when unknown (a Shortcut). Capability
  `turn-surface`.
- The report is kept on Hermes' in-memory record of that chat (up to 8, for
  15 minutes) and taken by the turn whose message has that hash, when the
  turn came from Conduit's socket. A message that steered or redirected a
  running turn never labels a later one (a goal's next step, a background
  result), and a turn that merges several queued messages, or one Hermes
  rewrote, gets no line. The plugin's own Watch jobs and call chats name
  the Watch themselves.
- `prompt.submit` refuses keys Hermes doesn't know, so the value can't ride
  there. With the gateway out of this process (`plugins.isolation: host`, a
  compute host) the route answers `"recorded": false`, and a turn on a chat
  that Desktop and Conduit both have open isn't placed as Conduit's
  (reply_sources), so neither gets a line, as before.

## Predictions for Conduit (plugin 0.80+)

Conduit's Predictive settings (all off until you turn them on) suggest your
next message after a reply, offer words for a draft when you pause typing,
and let Hermes take a turn of its own while you look at a chat. Each answer
comes from a fork of the live chat, as Hermes' `/btw` does: the same prompt
cache, every tool refused, nothing saved to the chat, state.db or memory.
Unlike `prompt.btw`, whose answer goes to every client (Hermes Desktop shows
it as a `[btw …]` note), the route answers only the device that asked.

- `POST /api/plugins/conduit_push/predict` with
  `{"session_id": "<runtime session id>", "kind": "replies" | "draft" | "turn", "count": 3, "language": "French", "model": "chat" | "fast", "wait_s": 20}`.
  `draft` adds `"draft": "<unsent text>"`; `turn` adds
  `"facts": {"last_reply_s": 192, "on_screen_s": 160, "check": 2, "max_checks": 4, "local_time": "Sunday 09:14"}`,
  measured by Conduit and written out in words, so the model never
  calculates. Answers, best first and each with a probability `p`:
  `{"replies": [{"text", "p"}]}`; `{"next": [...], "replies": [...]}` for a
  draft; `{"take_turn", "why", "ask_again_in_seconds", "nudge"}` for a turn,
  where `nudge` is the hidden user turn Conduit sends (`display_kind:
  "hidden"`) and `ask_again_in_seconds` is held to 30 s–15 min (`null`: not
  until the user writes). An answer that isn't the JSON asked for comes back
  with `"parsed": false` and nothing to suggest. Capability `predict`.
- One prediction at a time per chat (429), at most 4 running on the host
  (503), 40 a minute for the whole host. One that outlasts `wait_s` answers 504 and
  is left to finish; its chat is free again when it does. `GET …/predict`
  answers `{"fast_model": "<model>" | null, "today": <count>}`.
- `"model": "fast"` answers on `auxiliary.conduit_predict` (`provider`,
  `model`) from the chat's profile config, from a digest of the chat instead
  of the warm cache; without it the chat's own model answers. Usage is
  billed to the chat, like `/btw`.
- The chat must be live in this dashboard (404 otherwise). On a Hermes without
  the fork's parts the route falls back to its plain side question.

## Files in group chats (plugin 0.38+)

A group chat room isn't a session, so Hermes' attach RPCs can't take a file
for it. Conduit uploads photos and files picked in a room's composer with
`POST /api/plugins/conduit_push/room-files` (`{"name": "photo.jpg",
"data_base64": "…"}`, up to 50 MB) and puts the `@file:` reference it gets
back (`{"ok": true, "path": "…", "ref_text": "@file:…"}`) on its own line
above the message, as Hermes Desktop does for a chat. The room's members run
on this host and read the file from there. Uploads go to
`<hermes home>/conduit/room-files/<id>/` and are deleted a month later, or
sooner, oldest first, once they pass 2 GB together (checked hourly). An
upload that would leave under 1 GB free on the host is refused (507).
Capability `room-files`.

## Group pictures (plugin 0.41+)

Hermes' gateway-hosted group rooms carry no picture, so Conduit keeps a
room's custom picture here. `GET /api/plugins/conduit_push/room-images`
returns each room's revision (`{"ok": true, "revisions": {"<room id>":
"<rev>"}}`), so Conduit fetches only pictures it hasn't cached with
`GET /room-images/<room id>` (`{"rev": "…", "mime": "image/jpeg",
"data_base64": "…"}`). `PUT /room-images/<room id>` with `{"data_base64":
"…"}` sets a JPEG or PNG of up to 512 KB; `DELETE` clears it. Records live in
`<hermes home>/conduit/room-images/` (a small record plus the picture per
room, so listing never reads picture bytes), up to 500 rooms (the
oldest go first). Capability `room-images`.

## Group rooms and memory (plugin 0.62+)

In a gateway-hosted group room, each member's turn runs on its own host in a
hidden session (`source: bot_room`, titled `Group: <room id>`), and the
turn's message is the room's new messages, from several people and bots.
Hermes hands that turn to the profile's external memory provider (Honcho,
Hindsight and the like) with no author, so the provider would file everything
said in the room as the owner's own words. The plugin keeps room sessions
from saving into the provider: the per-turn sync, the session-end extraction
and the provider's part of the summary before a compaction are skipped for
them, also after a compaction moves the session to a new id, and for a helper
agent the room's session delegated to (Hermes runs no memory provider in one
today, so that's in case it ever does). Recall still runs, and `MEMORY.md` and `USER.md` still load, so a member knows its owner
in the room; it just doesn't learn from what others say there. The memory and
skill tools work as before, except in a room with other people's agents (next
section) where they're blocked. If Hermes' memory manager lacks one of the
methods the plugin guards, the plugin guards the rest and logs a warning
naming what's missing. If Hermes' session store (`state.db`) can't be read
when the plugin needs it to tell whether a session is a room, the plugin logs
a warning and treats that session as an ordinary chat.

## Long group room threads (plugin 0.77.6+)

Hermes plans a gateway-hosted room's next turn from the last 24 messages of
each thread. Before planning, it matches each member reply among them to the
user message the reply answered. Once the oldest of those messages is a reply
whose user message was left out, the match fails on every attempt, so the room
gets no more turns, in that thread or any other, and Hermes logs nothing. The
plugin fills in each left-out user message for that match: its place in the
room log is the seq the reply's turn id starts with (Hermes builds turn ids
from it), so the planner plans the same turns it would with the whole thread.
The plugin's gateway load guards the planner there, and its dashboard routes
guard it in a dashboard that runs as its own process and hosts rooms itself
(with `plugins.isolation: host` the routes load in a plugin host process
instead, so they don't guard that dashboard's rooms). Anything else wrong with
a thread still fails the way Hermes fails it. The plugin logs once, at info
level, the first time it fills a message in, and leaves the planner alone,
saying so at info level, when Hermes' planner has no
`_derive_member_watermarks`.

## Group rooms between turns (plugin 0.84.1+)

Hermes' room driver looks at each gateway-hosted room on a thread of its own,
up to four at once. A change, such as a message or a reply, wakes the driver
and marks every room whose pass is still running, for one more pass once it
ends. Hermes asks for that pass by waking the driver the same way, which marks
the passes still running then. So with two rooms or more, their passes can
keep marking each other, and the driver looks at every room over and over,
using CPU, until Hermes restarts. With the plugin, a pass that ends only wakes
the driver, which then looks at its room again, and any other wake still
marks every room being looked at. The plugin's gateway load guards the driver
there, and its dashboard routes guard it in a dashboard or `hermes serve` that
runs as its own process and hosts rooms itself (with `plugins.isolation: host`
the routes load in a plugin host process instead, so they don't guard that
process's rooms). A Hermes whose room driver has no `wakeup` or
`_run_room_once` logs one line, at info level, and keeps its own behaviour.

## Artifacts (plugin 0.40+)

Conduit's Artifacts screens list what a profile's chats produced:
`GET /api/plugins/conduit_push/artifacts` reads the profile's `state.db`
(read only) newest message first and returns
`{"ok": true, "items": [...], "next_before": 1234}`. Each item has a `kind`
(`image`, `video`, `audio`, `page`, `document`, `file` or `link`), a `source`
(`delivered` for a `MEDIA:` file, `preview` for a `::preview` page, `written`
for a document or media file a tool wrote, including a picture
`image_generate` saved (plugin 0.71.1+; a picture it reports by URL has source
`link` and kind `image`), `attached` for a file the user sent
with `@file:`, `link` for a web link in a reply), a `name`, the chat's
`session_id` and `session_title`, the `message_id` it came from and its
`timestamp`; a file has its `path`, whether it still `exists` and its `size`,
a link its `url` and `host`. Within a page one path or URL is listed once, at
its newest mention (a later page can list it again at an older one). Query: `session_id` (one chat; repeat it for each id the chat is
known by, and the sessions it continues from or spawned are read too), `kind` (`images`, `pages`, `files` or
`links`), `q` (words in the name, chat title, path or URL), `limit` (up to
200) and `before` (the previous page's `next_before`; `null` once the history
is read to its start). A request reads a bounded window of rows, so a page can
come back short with a `next_before` to keep going. One message adds at most 200
artifacts to a page (a page ends after the message that fills it, so it can
run past `limit`); the rest of a message naming more than that isn't listed. With `session_id` (plugin
0.43.2+) only that chat's own rows are read, so a chat under a long history
of other chats, cron runs say, lists in one request. `missing=hide` (plugin
0.51.8+) leaves out files no longer on the host, so a history of deleted files
doesn't leave pages mostly empty; links always stay, and an older plugin
ignores it and lists them. Hidden chats (a group room member's turns, say)
aren't listed, except a bot's Bot Chat, the hidden session titled "Bot Chat"
in its profile, and the sessions continuing it; a chat asked for by
`session_id` is read even when hidden (plugin 0.78.1+; before, a Bot Chat
listed nothing). Capability `artifacts`.

## Hermes calls you (plugin 0.13+)

In a Live Voice call you can hand Hermes a long job and say "call me when
it's done". Conduit asks this profile right away to watch the job's own
Hermes session, held while the call goes on. The first time a turn of that
session ends after you hang up, the plugin sends a **call request** through
the relay instead of the usual "Response ready" or "Turn failed" push: a
"Hermes wants to talk" notification whose Talk button opens Live Voice in the
job's chat, where Hermes opens with how the job went. With relay 0.9+ and a
Conduit build that rings, it arrives as a native incoming call (CallKit)
instead, and the notification is the fallback. With relay 0.10+ and a Watch
app that rings, the Apple Watch rings too, and answering on either stops the
other (see Relay API).

While the call goes on, Conduit renews the hold every minute and releases it
when you hang up. A job that ends during the hold gets its usual push and the
call waits: Conduit tells you in the call (and removes the watch), or, if you
hang up first, tells you itself when it releases the hold. If the phone goes
away mid-call (crash, no signal), the hold runs out and the plugin calls on
its own, so the call never depends on the phone reaching this host after the
call.

Calls are off until you turn them on in Conduit's Settings, under Calls from
Hermes. Settings and watches live per profile in
`<hermes home>/conduit-calls.json` (mode `0600`), shared by the turn-end hooks
and the dashboard routes under one file lock, so they survive restarts:

| Setting | Default | Range |
| --- | --- | --- |
| `enabled` (Hermes can call me) | off | |
| `when_asked` (call when I explicitly ask) | on | |
| `decides` (Hermes decides when to call, 0.14+) | off | |
| `alerts` (approval, question and failure calls, 0.14+) | off | |
| `min_gap_s` (time between calls) | 120 s | 30–3600 s |
| `per_hour` | 6 | 1–30 |
| `per_day` | 20 | 1–60 |
| `rules` (what's worth a call, 0.16+) | empty | up to 500 characters |

| Method | Route | Body | Returns |
| --- | --- | --- | --- |
| GET | `/api/plugins/conduit_push/calls?profile=` | | `{ok, paired, settings, bounds, rules_max, watches}` |
| PUT | `/api/plugins/conduit_push/calls?profile=` | `{settings: {...}}` (any subset) | `{ok, settings}` |
| POST | `/api/plugins/conduit_push/calls/watches?profile=` | `{session_ids: [...], title, hold_s?, ended_within_s?}` | `{ok, status: "watching", id}` or `{ok, status: "ended", outcome}` |
| PUT | `/api/plugins/conduit_push/calls/watches/{id}?profile=` | `{hold_s}` (0 releases) | `{ok, status: "watching"}`, `{ok, status: "ended", outcome}` or `{ok, status: "gone"}` |
| DELETE | `/api/plugins/conduit_push/calls/watches/{id}?profile=` | | `{ok, removed}` |
| PUT | `/api/plugins/conduit_push/calls/presence?profile=` (0.14+) | `{hold_s}` (0 releases) | `{ok, status: "present"}` or `{ok, status: "away"}` |
| POST | `/api/plugins/conduit_push/calls/outcomes?profile=` (0.15+) | `{call_id, session_ids: [...], outcome: "declined" or "missed", kind?, title?, reason?, age_s?}` | `{ok, status: "recorded"}` |
| GET | `/api/plugins/conduit_push/calls/waiting?session_id=&profile=` (0.70+) | | `{ok, waiting}`: null, `{kind: "question", questions: [{number, question, choices, multi_select, answered, may_leave_blank}]}` (`may_leave_blank` 0.82+) or `{kind: "approval", description, command, command_cut, answered}` |
| POST | `/api/plugins/conduit_push/calls/waiting/answer?profile=` (0.70+) | `{session_id, answer?, question?, choice?, reason?}` | `{ok, status: "answered", ...}`, `{ok, status: "not_waiting"}`, `{ok, status: "refused", message}`, or for an approval Hermes didn't take (0.70.1+) `{ok, status: "too_late" or "failed" or "unconfirmed", message}` |

- With alert calls on, what Hermes waits on is kept too (0.70+): a question
  batch's choices, or an approval's description and command (up to 600
  characters). A call answered with no chat open, the Apple Watch's, reads it
  from `/calls/waiting` and sends the user's spoken answer to
  `/calls/waiting/answer` (or `answer_call` on its Watch grant, which
  approves or denies only with `answer_approvals: true`; on Local Live the
  host takes the user's words itself, a plain yes or no for an approval). A question
  takes `answer`, a choice's number or label, "the recommended one", or the
  user's own words, for `question` (its number in the batch; the first
  unanswered by default), and answers the next one with `next`. A batch
  Conduit shows as a form (several questions, at least one with no choices)
  marks each question `may_leave_blank` (0.82+): "skip", "leave it blank"
  and the like in the app's languages leave it blank, as the form's field
  left empty, and Hermes reads it as skipped (`answer: "left blank"`,
  `skipped: true`); a choice named whole by the words ("Pass") still picks
  the choice. The clarify
  loop takes the batch once every question has an answer. An approval takes
  `choice` (`once` or `deny`, with `reason` for a deny): the agent's process
  resolves the request with that command, never another one queued beside
  it, and a command too long to read out whole can only be denied. The
  answer waits up to 10 seconds to hear how it fared (0.70.1+), so the call
  never says "approved" when nothing ran: `answered` once Hermes took it,
  `too_late` when no pending request was the one read out (answered on
  its card, or timed out), `failed` when nothing picked it up in time (it's
  taken back, so it can't land later), `unconfirmed` while still being
  passed on. Gone once answered anywhere, and after 2 hours.
- A watch names up to 4 session ids (the job's runtime and stored ids) and a
  title of up to 120 characters. It fires once, on the first turn end of any
  of them: `done` (the turn finished), `failed`, or `stopped` (interrupted).
  A replayed hook finds nothing left, so a job calls at most once. Watches
  expire after 24 hours; a profile holds at most 20 (429 past that).
- `hold_s` (0–600) holds the watch while the call goes on. A job that ends
  during the hold keeps its outcome; releasing the hold (`hold_s: 0`) then
  answers `ended` and consumes the watch, and a hold that runs out calls.
  The waiting runs in the agent process, and every turn end also sweeps for
  overdue calls, so a gateway restart doesn't lose one.
- `ended` from POST means one of the job's sessions ended within the last
  `ended_within_s` seconds (how long ago the job's request went out, on the
  phone's clock, so an earlier turn of the same chat doesn't count; the plugin
  remembers turn ends for 30 minutes while calls are on); Conduit then tells
  you itself.
- 409 when the profile isn't paired with Conduit, or calls (or "call when I
  ask") are off. 400 for an unknown setting or a value outside its range.
  429 past 30 requests a minute per profile.
- A call held back by your limits, or calls turned off after the watch was
  made, consumes the watch and sends the usual push instead. So does a relay
  that refuses the call request (relays before 0.8 don't know it; 0.8 caps
  call requests at 60 a day per installation, `call_limit`), one that took
  the request but couldn't reach Apple, or one that stays unreachable or keeps
  failing (5xx) after three tries (2 s and 4 s apart, same event id); a cron
  job's worker waits up to 10 seconds for those tries before it exits
  (0.81.2+), so a relay that hangs longer loses both. One
  malformed session id drops the job details from the request, on the host
  and on the relay alike; it still opens the chat.
- **Presence (0.14+):** while you're in a Live Voice call, Conduit holds
  presence (`hold_s` up to 600, renewed during the call, 0 at hang-up).
  Nothing rings meanwhile: a call that would go out sends the usual push.
- **Declined and missed calls (0.15+):** when you decline a call, or it
  rings out, reaches a phone that was offline or is silenced by Do Not
  Disturb, Conduit reports it here with the call's id, session ids, kind,
  title and reason, and `age_s` (how long ago, on the phone's clock). The
  next turn of one of those sessions gets a one-line note through the
  `pre_llm_call` hook, for example "the user declined the phone call you
  placed 12 minutes ago", and takes it, so Hermes hears it once, even if a
  late retry of the report arrives after. Nothing rings again. A retried
  report keeps the first; a profile keeps at most 20 waiting, each for a
  day. 409 when the profile isn't paired, 400 for anything malformed.
- **Conduit first (0.36+):** every turn of a main conversation also gets one
  line naming what the phone shares with this profile (calendar and
  reminders, health, location, phone status, contacts) and saying to use
  the Conduit tool before other tools, skills or CLIs for the same data,
  such as gog. Each of those tools' descriptions ends the same way. Hermes
  drops `pre_llm_call` context after the turn, so the line never piles up;
  what's shared is looked up at most once a minute. Subagents don't get it.
- The call request carries the job's id, outcome, title and session ids so
  Conduit can open the right chat. With end-to-end encryption they are sealed
  like any other content; with `redact on` the title is dropped.
- **Ringing (relay 0.9+):** a phone that registered a PushKit token
  (`voip_token`) gets the call request as a VoIP push on the app's `.voip`
  topic, with no alert, the same routing or sealed envelope, and `sent_at`,
  so it rings as a native call. Like a notification, APNs tries to keep it
  for a phone that's offline (for up to a day; best effort, and only the
  newest one per app), and Conduit shows one that arrives late as a missed
  call rather than ringing. Anything that stops it ringing
  (APNs refusing or unreachable, a call with no chat to open) sends the usual
  "Hermes wants to talk" notification instead, and a PushKit token APNs calls
  gone is forgotten until the phone registers a new one. The
  relay answers `rang: true` when APNs took the VoIP push. Calls carry a `reason` of up to
  200 characters (dropped with previews off, like the title), and the kinds
  `approval` and `question` besides how a job ended.

### Hermes asks to call (plugin 0.14+)

The plugin gives Hermes a `conduit_call_user` tool (toolset `conduit`) and a
bundled skill, `conduit_push:calling-the-user` (plugin skills aren't listed in
the skill index, so the tool's description points to it). Hermes calls it
with a `reason` (one spoken sentence, up to 200 characters) and
`asked_by_user`:

- The call rings when the asking turn ends, so it opens on the finished
  result: the turn's final reply is in the chat the call opens, and the
  reason is said first. This works from any chat Hermes runs in (typed
  chats in Conduit or Desktop, other platforms, cron jobs), not only Live
  Voice.
- `asked_by_user: true` needs "call when I ask"; `false` needs "Hermes
  decides". Either way the limits apply, presence holds it, and a session
  already watched keeps one watch (it takes the reason).
- **What's worth a call (0.16+):** with `rules` written (the user's own
  words, such as "only if production is down"), a call Hermes decides on
  first answers `check_rules` with them. It goes only when Hermes calls again
  with `fits_rules: true` in the same chat, within 10 minutes, while the rules
  are unchanged. Calls the user asked for skip the rules, and Hermes can read
  them but not change them. The rules are checked when Hermes confirms the
  call, not when it rings, and they steer Hermes rather than bind it:
  `asked_by_user` is Hermes' own word (0.14+), so a call it marks as asked
  for skips them.
- **Call watches (0.17+):** the rules only filter the calls Hermes makes;
  they don't watch anything. When the user asks to be called if something
  happens ("call me if the homelab is down for more than 10 minutes"),
  Hermes uses `conduit_call_watch` and only describes the watch: `what`,
  `how` a run checks it, `for_minutes` it must last, and optional `hours`
  (HH:MM-HH:MM) and `unless`. The plugin writes the cron job through
  Hermes's own `cronjob` function: named "Call watch: <what>", checked every
  few minutes (picked from `for_minutes`), with a fixed run prompt. The same
  `what` is the same watch, so asking again changes it instead of adding
  another. `sync` takes every watch the What's worth a call note describes
  (Conduit's "Have Hermes watch for this"): it changes and adds watches to
  match and lists the ones the note no longer mentions, which Hermes asks
  about before removing.
- **A watch's runs:** each run checks and calls `conduit_watch_report` with
  whether it's happening. The plugin keeps the incident per job in
  `conduit-call-watches.json`, waits out `for_minutes` and the allowed hours,
  calls once per incident with the run's sentence as the reason, and tells
  the run what to reply (`[SILENT]` while there's nothing to send).
  `conduit_call_user` refuses in such a run. Watches set up by 0.16.x (jobs
  that load the skill and keep their own state) keep running as written, and
  setting one again by name or id moves it onto the report.
- A call the user asked for while calls are off answers `calls_off` with a
  `conduit://settings/calls` link for Hermes' reply, which opens Calls from
  Hermes in Conduit.
- The tool shows only while this profile is paired and one of those is on.
  Subagents can't call; their parent can. `conduit_call_watch` shows while
  the profile is paired on a Hermes with scheduled jobs, and
  `conduit_watch_report` while it's paired (it only works in a watch's run).
  When `conduit` is off for cron in `hermes tools`, each watch's job gets
  its own toolset list (cron's, plus `conduit`) so its runs can report.
- If `hermes tools` lists the `conduit` toolset as off for a platform, turn
  it on there.

### Alert calls (plugin 0.14+)

With `alerts` on, Hermes also calls when it waits on you:

- **Approval:** an approval request still unanswered after a minute rings
  (kind `approval`, the request's description as the reason), beside its
  usual answerable notification. Answering it anywhere first
  (`post_approval_response`) or the turn ending cancels the call.
- **Question:** the same for a clarify question (kind `question`), answered
  through Conduit's card, Desktop or the CLI. Needs the clarify middleware
  (Hermes with `register_middleware`).
- **Failed turn:** a turn that fails calls in place of its "Turn failed"
  push (kind `failed`).

Alert calls count against the same limits. In the call, Conduit can read the
approval out and answer it by voice.

## Hermes changes Conduit settings (plugin 0.19+)

Ask Hermes to set Conduit up ("make live calls more relaxed", "use Gemini
Live with my memory") and it can, through the `conduit_settings` tool, within
what Conduit allows:

- **Conduit decides what Hermes sees.** Each Conduit uploads the settings it
  shares (key, plain-language title and meaning, choices, value and tier) and
  the user's **Let Hermes change settings** mode to
  `PUT /api/plugins/conduit_push/conduit-settings` when it connects, comes to
  the foreground, changes a shared setting, or a turn used the tool. Anything
  it doesn't list (keys, wake phrase setup, Calls from Hermes, Watch access,
  the mode itself) is out of Hermes' reach.
- **Hermes only asks.** `get` lists the settings; `change` keeps a request
  with the value Hermes saw and a reason; `cancel` takes back one Conduit
  hasn't picked up. Requests wait in the profile's `conduit-settings.json`
  for a day.
- **Conduit applies or asks.** It checks every request against its own list
  again. With the default mode, **Ask me every time**, each change waits for
  the user's Apply. With **Small changes without asking**, the small tier
  (how calls sound, voices) applies with an Undo and the rest still asks.
  **Off** refuses everything. A setting the user changed after Hermes read it
  keeps the user's value. Conduit reports each outcome (applied, dismissed,
  conflict, refused, undone) on its next sync, and `get` shows it to Hermes.
- Speech providers and their keys, `voice.gpt_live` and models are Hermes
  settings, not Conduit's: the tool tells Hermes to change those in its own
  config.

The tool shows only once a Conduit has synced with the profile. Limits: 20
requested changes per device per hour, 10 per call, 8 devices per profile.

## Conduit preferences synced across devices (plugin 0.54+)

Every iPhone and iPad signed in to a profile shares Conduit's preferences
through this plugin: appearance, voice settings, CarPlay and the other
saved dashboards (label and address only). Conduit turns this on in
**Settings > Sync across devices**.

- **What's kept.** Conduit sends only the preferences it chooses to sync, to
  `PUT /api/plugins/conduit_push/conduit-prefs`, and stores nothing else
  here. Passwords, sign-in sessions, Cloudflare tokens, custom headers and
  per-device consent never leave the device. The values live in the
  profile's `conduit-prefs.json` (private to the host user).
- **Conflicts.** The later change wins, timed by this host's clock when it
  arrives.
- **Moving to another host.** Either open Conduit on the new host and choose
  **Settings > Sync across devices > Copy this device's settings here**, or
  copy the file across from the command line:

  ```bash
  hermes conduit-push prefs export conduit-prefs.json   # on the old host
  hermes conduit-push prefs import conduit-prefs.json   # on the new host
  ```

  `import` merges, with the imported values winning; add `--replace` to drop
  the ones the file doesn't list. Use `hermes -p <profile> conduit-push prefs …`
  for a profile other than the default. `prefs show` lists what's synced.

## Hermes asks you to confirm (plugin 0.34+)

Before an action you should OK (sending a message for you, buying, deleting),
Hermes can ask on your phone with the `conduit_present` tool:

- **Tool.** `confirm` keeps a prompt (title up to 80 characters, a Markdown
  preview up to 2,000, a button label up to 24, up to 5 `spoken_yes`
  phrases, `ttl_minutes` 30 by default and at most 1,440) and wakes the phone
  with a `phone.changes` push (`present:<id>`). `wait_seconds` (up to 120)
  waits for the answer. `status` returns waiting prompts and answers Hermes
  hasn't seen yet (or one by `id`); `cancel` takes one back. States:
  `waiting`, `confirmed`, `declined`, `edit`, `expired`, `cancelled`. Only
  `confirmed` means go ahead; on `edit` you send the corrected request
  yourself.
- **Conduit answers.** It shows a panel over everything, calls included, and
  you confirm by tap or, in a call, by voice. It reports answers and fetches
  waiting prompts with
  `POST /api/plugins/conduit_push/present/exchange?profile=`:
  `{"installation_id", "device", "app_version", "results": [{"id", "outcome": "confirmed|declined|edit", "edit_text"?, "via": "tap|voice"}]}`
  answers `{"ok": true, "prompts": [{"id", "title", "preview", "action_label", "spoken_yes", "session_id", "created_at", "expires_at"}]}`.
  The first answer wins; answers for unknown or finished prompts are ignored.

The tool shows only once a Conduit with `present-v1` has exchanged with the
profile; subagents can't use it. Prompts live in the profile's
`conduit-present.json` (answers are kept a week). Limits: 20 prompts an
hour, 20 waiting at once, 60 exchanges a minute per profile.

## Hermes shows things during a voice call (plugin 0.44+)

In a Conduit voice conversation (Local Live, Classic voice, or a live call
that hands work to Hermes), a table, steps, a chart or links are better seen
than heard. Hermes puts them on the call's screen with `conduit_show`
(`title` up to 80 characters, `markdown` up to 8,000) and says in a sentence
that they're there. Voice turns get a note saying so while a Conduit that
shows cards is connected.

- **No push.** The card is kept in `conduit-present.json` and listed in the
  same `present/exchange` answer as `"cards": [{"id", "title", "markdown", "session_id", "created_at", "expires_at"}]`
  (only when there are any). Conduit exchanges as soon as it sees the tool
  in the turn and again when the reply ends, and shows each card once.
- **Short-lived.** A card is dropped after ten minutes: it belongs to the
  conversation it was shown in.

The tool shows only while a Conduit that says `show-cards` in its exchange's
`features` has exchanged with the profile in the last six hours, and only
that phone gets the cards; subagents can't use it. Limits:
60 cards an hour, the newest 20 kept.

### Map and place cards (plugin 0.45+)

`conduit_show` also takes `cards`: up to four maps, places and other cards Conduit draws
natively, in a typed chat as well as on a call. `markdown` is optional with
cards; without it, the cards' own Markdown (each place with an Apple Maps
link) is what a phone that doesn't draw cards, or another client, shows.
A call-screen card whose Markdown is only that text carries `"fallback": true`
in the exchange (0.45.1+), so a phone that draws the cards leaves it out.

- **One schema.** [`cards/card-schema.json`](cards/card-schema.json) is the
  only definition. The tool's `cards` parameter is built from it and
  `card_schema.py` normalizes against it. Conduit ships a byte-identical copy
  for the live models' `show_on_screen` and fails its CI when the copy
  differs from main; [`cards/card-vectors.json`](cards/card-vectors.json)
  holds examples both test suites run. Change both files together and merge
  here first.
- **Sourced facts only.** The phone adds address, category, phone, website,
  travel time and Look Around from Apple Maps when it draws a card; the
  schema has no fields for them. A rating, hours, price or photo needs a
  `source`, or it's dropped; a place's or person's photo also needs an https
  `url`.
- **Kept in history.** The tool's answer carries the normalized cards
  (`{"ok": true, "cards": [...]}`), so the chat's history keeps them and
  Conduit draws them whenever the chat is opened. With no call screen, a
  Conduit that says `chat-cards` in its exchange's `features` still gets
  the tool, and the card shows in the chat only. A turn that isn't from
  Conduit (Desktop, or a messaging app through Hermes' gateway) is refused
  with `not_conduit`. A classic `hermes` terminal turn can't be told apart
  yet (Hermes doesn't bind its platform for tools), so it isn't refused.
- **Live calls.** A saved call's assistant turn can carry `cards`
  (`POST /voice/sessions`): kept with a chat's call and returned by
  `GET /voice/chat-calls`; in a call saved as its own row, written as their
  Markdown after the turn's text.

### Information cards (plugin 0.46+)

Cards can also be `stats` (big numbers with a change and a small line),
`chart` (line, area, bar or pie, drawn with Swift Charts), `weather` (now,
the next hours and days), `comparison` (two to four options, one
recommended at most) and `steps` (what you need, then steps the user can
tick off, with timers). Each type has its own field, and the schema's rules
keep only that field.

- **Where it's from.** These cards take a card-level `source` (`name`, and
  a `url` if any), shown on the card. A forecast needs one or it's dropped;
  without one, the other cards read as Hermes' own.
- **Only where they're drawn.** A Conduit says `info-cards` in its
  exchange's `features` when it draws them. Unless every Conduit that
  draws cards (`show-cards` or `chat-cards`) and exchanged with the profile
  in the last 30 days says it, the tool leaves them out (`unsupported_cards`
  when nothing else is left): the chat keeps the cards, and an older build
  opening it would show them as nothing. Each phone's exchange
  also leaves out the cards it can't draw and shows the Markdown instead.

### Media, contact and live cards (plugin 0.47+)

- **`media`**: up to 12 photos, videos or audio clips, as direct https links
  (`kind`, `url`, and `title`, `by`, `caption`, `duration`, `artwork`,
  `page`). Conduit shows photos in a gallery and plays video and audio
  itself. A media card needs a `source`.
- **`contact`**: up to six people (`people`: `name`, `detail`, `phone`,
  `email`, `url`, `address`, `note`, and a `photo` with its source). Conduit
  offers Call, Message, Email and Add to Contacts, and says "In your
  contacts" when the person matches one the user already has (checked on
  the phone, only when Conduit already has access). A phone or email that
  doesn't read as one is dropped. A person can carry a `message`, a text
  Hermes wrote for the user to send them (plugin 0.67+, only with their
  `phone`): Conduit shows it with Send text, which opens Messages filled in,
  and only the user's tap sends it. A Conduit that says `text-drafts` in its
  exchange gets it; for any other, the tool leaves the text off the card and
  tells Hermes to give it in its reply.
- **`status`**: where something in progress stands (`state`, `phase`
  waiting/active/done/problem, `progress` 0–1, `detail`, `eta`,
  `milestones`).
- **Live names.** Any card can carry `live`, a name Hermes picks. Showing a
  card with the same live name again in the conversation replaces the
  earlier one, on the call screen and in the chat, so Hermes keeps a
  delivery, a score or a job's progress up to date by showing it again.
  Cards shown beside the earlier one stay.
- A Conduit says `media-cards` and `live-cards` in its exchange when it
  draws them; the same gating as information cards applies.

## Health data from Conduit (plugin 0.23+)

Conduit can share Apple Health data with a Hermes profile so the agent can
answer "how did I sleep?" or "how active was I this week?". It's off until
the user turns it on in Conduit, per category (activity, heart, sleep,
workouts, body, vitals, fitness), with how far back to start.

- **Straight to this host.** The phone posts to
  `POST /api/plugins/conduit_push/health/ingest` over your dashboard
  connection. Health data never passes through the push relay or any
  Conduit server.
- **Sealed end to end.** Each phone agrees a key with the profile once
  (`POST /health/key`, X25519: only public keys cross the wire, so watching
  that exchange doesn't reveal the key). Every batch is sealed with
  ChaCha20-Poly1305 under an HKDF-SHA256 key from it, on top of any TLS, so
  even a dashboard on plain http in your LAN never carries health data in
  the clear. Like any plain-http service, someone actively tampering with
  the connection while the key is set up could still intercept it: use
  HTTPS or a private tunnel when you can.
- **Who can use these routes.** Anyone signed in to your dashboard, as
  for every other dashboard route; push pairing isn't needed. A signed-in
  client could register a new key under a phone's device id and send data
  in its name (the phone notices on its next sync and sets up a new key),
  so treat dashboard access as access to your health data. A new device key
  for a device id drops what the earlier key sent (the phone sends it all
  again), so a re-key can't be used to read a phone's data back; the same
  device key again keeps it, since only that phone can use it.
- **Refusals.** A request the store refuses answers with its HTTP status
  and `{"detail": "<message>", "code": "<word>"}`. `unknown_key` and
  `bad_seal` (409) mean this host doesn't have the phone's key (the phone
  sets up a new one); `too_many_devices` and `schema` (also 409) don't. The
  location routes answer the same way. Other errors carry only `detail`.
- **Summaries first.** Conduit sends the hourly and daily totals Apple
  Health computes on the phone (iPhone and Watch already merged), a summary
  per night of sleep, sleep stage segments and workouts. Raw readings go
  only for categories where the user turns on detailed readings.
- **Stored here only.** The profile's `conduit-health.sqlite3`, mode `0600`.
  A newer computation of a window replaces the older one; a deleted workout
  or reading stays deleted. `GET /health/status` reports counts and dates,
  never values; `POST /health/wipe` deletes one device's data and key (or
  everything with `{"all": true}`), and Conduit's "Delete health data on
  this host" calls it. Only the voice read below returns health data.
- **Read-only tool.** `conduit_health` (actions `overview`, `daily`,
  `hourly`, `sleep`, `workouts`, `samples`; at most 92 days per call) with
  its skill `conduit_push:reading-health-data`. It shows only once a phone
  has synced data, and subagents can't use it. What it returns goes to your
  agent's model like any tool result, which Conduit tells the user before
  they turn sharing on.

Limits: 4 devices per profile, 6,000 items and 4 MiB per batch, 120 uploads
a minute per profile.

### Voice reads (plugin 0.25+)

Gemini Live, Grok Live and GPT-Live run at Google, xAI and OpenAI, driven by
Conduit, so they can't call Hermes' tools. When the user lets a voice mode read
their health data in Conduit's Settings > Health (off by default and asked per
mode, naming the voice provider, since that company may not be the one Hermes
uses), Conduit offers the voice model `conduit_health` with Hermes' own
description and parameters (minus `device`: a voice read covers only the
asking phone), and runs each call here. That switch lives in Conduit; the
host's boundary is the seal:

| Method | Route | Returns |
| --- | --- | --- |
| GET | `/api/plugins/conduit_push/voice-tools` | Tools this profile can answer for voice (name, description, parameters, which key seals them); empty until a phone has synced |
| POST | `/api/plugins/conduit_push/voice-tools/call` | One sealed read: `{v: 1, tool, device_id, kid, call_id, nonce, ct}` in, `{v: 1, nonce, ct}` out |

- **Sealed both ways with the device's own health key**, with its own HKDF
  directions (`conduit-health-v1 voice device-to-host` / `host-to-device`)
  and associated data binding the tool, key, device and call id. Dashboard access
  alone opens nothing.
- **Only the asking device's data**, matched by id (never by name), so
  another device or a newly registered key reads nothing.
- **Never across keys.** If the device registers a new key while a read
  runs, the answer is refused with `unknown_key` (409) instead of being
  sealed to the old key. The calendar, phone and location reads, and the
  phone's changes exchange, do the same (plugin 0.51.3+).
- Read actions only (the same handler as `conduit_health`), 60 reads a
  minute per profile. Conduit keeps the log of what each call read.
  Shared vector: `tests/health_vector.json` (`voice`).

Calendar and phone status (plugin 0.29+): once a phone shares them,
`conduit_calendar` (read actions only: overview, calendars, events, free_busy,
next, reminders) and `conduit_phone` (status) join the list with `key:
"phone"`. Their call is the phone feature's sealed exchange with `op:
"voice"` (`{v: 1, device_id, kid, call_id, op: "voice", nonce, ct}`, the
tool's name inside the seal: `{call_id, tool, args, provider}` in, `{call_id,
result}` out), answered for the asking phone only. Conduit asks for this per
voice mode in Settings > Calendar & phone. Adding or deleting an event or
reminder in a call is the phone's own tool, with spoken confirmation (deleting
takes the item's id from these reads), so no write is offered here. With plugin 0.30, `conduit_phone` also offers `alarms` (the alarms and
timers Hermes set) and `conduit_contacts` (`people`) joins the list, each once
a phone shares it; setting an alarm or timer in a call is the phone's own tool
too.

## Events from Shortcuts automations (plugin 0.73+)

Conduit's **Log to Hermes** Shortcuts action keeps what an automation hands
over (a notification from an app the user picks, a message, an email, a
Wallet payment, an NFC tag, a sound the phone heard) on this host, for the
agent to look up later with `conduit_inbox` ("what did the bank send me
today?"). Logging starts no chat, sends no push and works with Pulse off.

With Pulse on, its report and (plugin 0.76+) its morning brief read what was
logged since the last report, and the user's watches (things they asked to
be told about, which Pulse memory keeps) are checked against new events as
they arrive: a run starts at most every 15 minutes, and later arrivals ride
the next one. A watch still open after a month asks if it's still wanted. A
report item, the brief or a watch's message may quote an event, but Pulse
memory never keeps other people's messages or facts about who sent them
unless the user asks.

- **Straight to this host, sealed end to end.** The phone posts to
  `POST /api/plugins/conduit_push/inbox/ingest` over your dashboard
  connection, after agreeing a key once with `POST /inbox/key`. It uses the
  same X25519, HKDF-SHA256 and ChaCha20-Poly1305 scheme as health and
  location; nothing passes through the push relay.
- **Sign-in codes are never kept.** The phone doesn't send anything that
  looks like a verification code, one-time password or sign-in link, and
  this host drops it again if one gets through.
- **Kept for a while, then gone.** Events stay 30 days (the phone may ask
  for fewer), at most 5,000 per phone, in
  `conduit-inbox.sqlite3` in the profile's home, readable only by its owner.
  A new key for a phone drops what the old key sent.
- **Data, not instructions.** What the inbox holds is other people's words.
  The tool tells the agent to treat it as data and not to forward it unless
  the user asks. Subagents can't read it.
- **Status, reading back and deleting.** `GET /inbox/status?device_id=…`
  returns counts only. Conduit's Settings page reads a phone's own events
  with `POST /inbox/list`, sealed for that phone alone under a second key
  from the same agreement, and deletes the ones the user picks with
  `POST /inbox/delete`. `POST /inbox/wipe` with `{"device_id": "…"}` or
  `{"all": true}` deletes everything a phone sent, or everything.

The app checks for the `shortcut-inbox` capability; on an older plugin the
action keeps events on the phone until the plugin is updated.

## Location from Conduit (plugin 0.26+)

Conduit can tell a Hermes profile where the user is. It's a building block
rather than one feature: the agent can answer "how far am I from work?",
follow place rules ("call me with my brief when I leave home on weekdays"),
use it from any scheduled job or skill the user teaches it ("check I'm on my
way before each appointment"), and Calls can skip places the user picked.
Two separate opt-ins in Conduit:

- **While talking** (When In Use): one fix when the user sends a message or
  starts a voice call, and again when they've moved over 200 m during a call.
  Precise by default.
- **In the background** (Always): arriving at and leaving saved places
  (up to 20), visits and significant moves. Area by default.

Each layer has its own precision: `places` (saved place names only), `area`
(about 1 km: neighbourhood and town, coordinates rounded to 2 decimals) or
`precise`. The phone applies it before sending and the host applies it again.
Saved places live on the phone: this host gets their names and kinds, never
their coordinates, and each fix carries distances to them instead. A place
marked private is reported only as "at a private place".

- **Straight to this host, sealed.** Same scheme as health with its own key
  and labels (`POST /location/key`, `POST /location/ingest`): X25519, then
  ChaCha20-Poly1305 under an HKDF-SHA256 key. Location never passes through
  the push relay; the relay only carries the on-demand request (below).
- **Stored here only.** The profile's `conduit-location.sqlite3`, mode
  `0600`. History is kept as long as the user picks in Conduit (latest only,
  1, 7 by default, 30 or 365 days). `GET /location/status` reports counts,
  never locations, and `locate.available` (whether `locate` can reach the
  phone: this profile is paired for notifications); `GET /location/reads` lists when the agent read location
  (kept 30 days, without the locations); `POST /location/wipe` deletes one
  device's data and key, or everything with `{"all": true}`. A new device
  key for a device id (a reinstall, sharing turned off and on, a new
  pairing) drops that device's fixes, visits and saved places; the phone
  sends its places again with the new key.
- **Tool.** `conduit_location` (actions `now`, `locate`, `places`,
  `history`, `distance_to`, `rules`, `set_rule`, `remove_rule`) with its
  skill `conduit_push:using-location`. It answers "paused" or "off" when the
  user has paused sharing, logs every read, and subagents can't use it.
- **Voice reads** (plugin 0.28): when the user lets a voice mode read their
  location in Conduit's Settings > Location (off by default, per mode,
  naming the voice provider), `/voice-tools` also offers `conduit_location`
  with only `now`, `places`, `history` and `distance_to` (key `location`),
  sealed as health's voice reads are but with the device's location key
  (`conduit-location-v1 voice device-to-host` / `host-to-device`). It reads
  only the asking device's fix (never its coordinates: area, saved place
  and distances only), places and history, matched by id, and each
  read is logged as "Gemini Live call" (or Grok Live, GPT-Live). Shared
  vector: `tests/location_vector.json` (`voice`).
- **On-demand locate.** `locate` asks the phone for a fresh fix through the
  relay as a silent location push (Apple's Location Push Service Extension),
  which the phone answers only while background sharing is on and not
  paused. At most one every 5 minutes and 30 a day per location store
  (profiles reading a shared one spend its budget).
- **Place rules.** `set_rule` keeps a Hermes cron job named
  `Place rule: <what>` that never runs on its own schedule; an arrive or
  leave event that matches the rule's place (one saved place or any), move
  (arrive, leave or either), days and hours triggers it with a line naming
  the place and move (`GET /location/rules`, `DELETE /location/rules/<id>`).
  The task is anything a cron run can do. A rule fires once a day, or every
  time at least 30 minutes apart (`repeat: "every"`), never for an event
  over 15 minutes old, and never while sharing is paused.
- **Other profiles on this host** (plugin 0.58+). Conduit binds sharing to
  one profile, which keeps the store, key and place rules. With "Share with
  this host's other profiles" on (`sharing.other_profiles: true`), every
  other profile on the same host that is paired with the same iPhone (the
  same relay installation in its `conduit-push.json`) reads that store:
  `now`, `places`, `history`, `distance_to`, `locate` and maps lookups. A
  locate or phone maps lookup goes through the asking profile's own pairing
  and names the sharing profile, so the phone answers it as that profile's.
  Reads are logged in the sharing profile's store as `<profile>: <chat>`.
  `rules`, `set_rule` and `remove_rule` answer `other_profile`: rules stay
  with the sharing profile. A profile that shares location itself (talking
  or background on) always reads its own, and if several profiles share
  with the others the first wins (default, then named ones alphabetically).
  The fresh-position and maps budgets belong to the sharing profile's
  store, so every profile reading it spends the same ones. Calls placed
  from a reading profile are held back by the shared position, but the
  places to avoid are that profile's own calls settings.
- **No calls at places.** Calls settings `avoid_places` (place ids) and
  `avoid_unknown_place` (also hold calls when there's no recent location,
  or sharing is paused or off): a call that would ring while the user is at an
  avoided place goes out as a notification (status `limited`, reason
  `place`). The latest fix counts for 24 hours with background sharing on,
  otherwise 1 hour.

Limits: 4 devices per profile, 20 places, 256 KiB per batch.

## Maps from the user's iPhone (plugin 0.31+)

`conduit_maps` gives Hermes Apple Maps: `search` (places or categories near
the user, a place or an address), `lookup` (one place or address),
`reverse_geocode`, `directions` (routes with traffic, tolls, steps, by car,
on foot, by transit or bike, and `leave_by` for `arrive_by`) and
`travel_times` (up to 10 destinations at once). Places can be `"me"`, a saved
place's name, an address or a name, `place_id:<id>` from an earlier answer,
or `lat,lon`. It's a building block like location: crons, place rules and
skills can use it.

`navigate` (plugin 0.37+) opens Apple Maps on the iPhone with directions to
a place. It's a confirm prompt (`conduit_present`'s store and exchange)
carrying `maps: {to, mode}`: Conduit opens Maps at once when the user is in
that chat with Conduit open, and otherwise shows a Go button and a
notification, answered by tap or by saying "go" in a call. Conduit says it
can do this with `features: ["maps-navigate"]` in its present exchange;
without one that said so in the last 30 days the tool returns an Apple Maps
link for a text chat. Only a Conduit that says so gets the Go prompt, and
only its answer counts.

The host has no map, so the user's iPhone answers. The tool stores the
lookup sealed for that one phone (location's key, its own HKDF labels) and
wakes it with the same location push as `locate`; the relay sees only a
request id inside the sealed push envelope. Conduit's Location Push
extension fetches the lookup (`GET /location/maps/{id}`), answers it with
MapKit and posts the sealed answer (`POST /location/maps/{id}`). The user's
own position stays on the phone: "from me" answers carry durations,
distances and places at the detail they chose for background sharing
("places only" refuses "me"). It needs background location sharing and a
Conduit that says it answers maps lookups (`sharing.features`), and the
tool is hidden until then. Lookups are logged in the location read log
(action and chat, never the query); budgets are 30 wakes an hour and 120 a
day, a repeat within 2 minutes is answered from the last answer, and when
the phone doesn't answer, a trip from "me" to a saved place gets a labelled
straight-line guess. Contract: `designs/maps-v1-contract.md` in the project.

Lookups that don't involve the user (or when the phone can't answer) go to
Apple Maps from this host instead, without waking the phone: the relay hands
paired hosts a short-lived Apple Maps Server API token (`POST
/v1/maps/token`; one cached token for every host, so it doesn't identify
one) and the plugin calls `maps-api.apple.com` itself, so the relay never
sees a lookup. Each host uses at most 200 Apple calls a day (a call counts
once the relay has given a token, and a retry after Apple refuses the token
doesn't count again). Saved places
(their coordinates live on the phone) and transit routes need the phone;
"me" on the host is the last shared fix at its detail. A user who turns on
"Keep maps lookups on this iPhone" in Conduit turns the host engine off for
that profile. A relay without a Maps key answers 404 and the host stops
asking for an hour. Self-hosted relays enable it with a key that has "Maps"
enabled for a Maps ID: put it at `deploy/secrets/maps/maps.p8` and set
`MAPS_KEY_ID` in `.env`.

## Calendar, Reminders, Focus and phone status from Conduit (plugin 0.27+)

Conduit can share the user's iPhone calendars (every account on the phone),
Reminders, which Focus is on and a snapshot of the phone (battery, charging,
Low Power Mode, CarPlay, audio route, network, motion) with a Hermes profile,
so the agent can answer "what's on today?", find a free slot, add the
dentist on Thursday, remind them to call mum when they get home, or keep it
short when the battery is low. Each kind is off until the user turns it on
in Conduit, with which calendars and lists, how far back and ahead, and
whether notes and attendees are included (off by default).

- **Straight to this host, sealed end to end.** The phone posts to
  `POST /api/plugins/conduit_push/phone/ingest` over your dashboard
  connection, never through the push relay. It agrees its own key with the
  profile (`POST /phone/key`, X25519, separate from health and location, so
  each can be on alone and wiping one never touches another), and every
  body is sealed with ChaCha20-Poly1305 under HKDF-SHA256 keys from it, on
  top of any TLS. The same caveats as health apply: use HTTPS or a private
  tunnel when you can, and treat dashboard access as access to this data.
- **What the user chose, enforced here too.** The phone says what it
  shares in every batch and the store holds it to that: no notes,
  attendees or links without "Include notes and attendees", no attendee
  emails without that switch, no events at all when the calendar is
  "add only" (write-only) or off, and status or Focus forgotten as soon as
  either is turned off. Only the shared window is kept (past days to
  future days); a batch whose events leave out the range stays inside the
  last one shared. `calendars` is the user's current selection, replaced
  whole: events and reminders on a calendar or list it leaves out are
  deleted. Status and Focus are the latest value only. A batch, status or
  Focus time more than 5 minutes ahead of this host's clock is refused
  with code `clock_skew`, so a future-dated batch can't make the real ones
  after it (a sharing change included) look out of date. What a refused
  batch turns off is still forgotten. A calendar or reminders shared
  "read only" (`calendar_read_only` / `reminders_read_only`, plugin
  0.52.0+) are read but never written: every change to them is refused
  with `shared_read_only`, changes already waiting fail at the phone's
  next exchange (as do those whose calendar or list is no longer shared),
  and a refused batch can't lift read-only.
- **Writes are changes the phone applies.** Hermes can't touch the phone's
  calendar, so `conduit_calendar`'s write actions queue a pending change.
  The plugin nudges the phone with a `phone.changes` relay event (sealed
  like any notification when the pairing has end-to-end encryption): a
  visible notification for a change the user approves on the phone
  (`mode: "alert"`), or a silent background push for an add the user lets
  Hermes make without asking (`mode: "background"`); updates, deletes and
  completions always ask, and adds to shared calendars or lists always ask.
  The phone picks the queue up and reports outcomes (applied, edited,
  dismissed, failed, expired) with one sealed `POST /phone/exchange`. The
  notification carries no calendar data beyond the sealed title and body:
  the change itself comes from this host. Pending changes expire after 7
  days, at most 50 per profile. A relay without `phone.changes` refuses
  the nudge; the phone still finds the change on its next exchange.
- **Stored here only.** The profile's `conduit-phone.sqlite3`, mode `0600`.
  `GET /phone/status` reports counts and dates, never content;
  `POST /phone/wipe` deletes one device's data, key and pending changes (or
  everything with `{"all": true}`).
- **Tools.** `conduit_calendar` (reads `overview`, `calendars`, `events`,
  `free_busy`, `next`, `reminders`; writes `add_event`, `update_event`,
  `delete_event`, `add_reminder`, `update_reminder`, `complete_reminder`,
  `delete_reminder` (Conduit builds before it leave that one waiting until
  it expires; deletes and completions carry the item's title and time so
  the phone's card names it);
  `pending` for how changes went) and `conduit_phone` (`status`), with the
  skill `conduit_push:using-calendar`. Each shows only once a phone shares
  what it reads; subagents can't use them. Reads say how fresh the copy is
  ("as of" the last sync). What they return goes to your agent's model like
  any tool result, which Conduit tells the user before they turn sharing on.

- **Alarms and timers (plugin 0.30+).** When the user lets Hermes set them
  (AlarmKit, iOS 26), `conduit_phone` gains `alarms` (the alarms and timers
  Hermes set that are still on: iOS shows an app only its own), `set_alarm`
  (one-off `at`, or `time` HH:MM with `repeats` days), `set_timer`
  (`seconds`) and `cancel_alarm`, as pending changes like calendar writes.
  They ring through Silent and Focus. Setting one goes without asking when
  the user lets Hermes add without asking; cancelling always asks.
- **Contacts (plugin 0.30+).** Only the people the user picks in Conduit
  (no address book permission): name, nickname, a relation the user types
  ("wife"), phone numbers, emails, birthday and organization, read with
  `conduit_contacts` (`people`, with an optional `query`). Turned off, they
  are deleted here.

Limits: 4 devices per profile, 5,000 items and 4 MiB per batch, 50 alarms and
200 contacts per device, 120 uploads and 60 exchanges a minute per profile,
64 KiB per exchange.

## Apple Watch lookups and jobs (plugin 0.6+, jobs 0.7+, Gemini tokens 0.8+, Qwen tokens 0.75+)

With the wrist down, a Conduit Watch call can't reach Conduit on the iPhone,
but it can reach the push relay over its own internet. For each call Conduit
asks this profile for a grant; the plugin opens it on the relay (see Relay API
above), long-polls the relay for the Watch's sealed calls, runs them here and
seals the answers back. This host opens no inbound route of its own.

| Method | Route | Returns |
| --- | --- | --- |
| POST | `/api/plugins/conduit_push/watch-tools/grant` with `{tools, max_jobs?, job_options?, job_profiles?, carry_jobs_from?, audio?, answer_sessions?, answer_approvals?}` | `{ok, grant_id, relay_url, key, watch_key, expires_at, tools, max_calls, max_jobs, job_profiles?, jobs_carried_from?, audio?}` |
| POST | `/api/plugins/conduit_push/watch-tools/revoke` with `{grant_id}` | `{ok, revoked}` |

- `tools` names what the call may run: `web_search` and `recall_memory`
  (the same code as the voice lookup routes), and with plugin 0.7
  `start_job`, `list_jobs` and `cancel_job` (granted with the Watch app's
  own `job_news` and `answer_approval`). A grant lasts at most 30
  minutes, holds one profile, and ends when the call does.
- **`answer_call`** (plugin 0.70.0; its approvals confirmed from 0.70.1) answers what Hermes waits on in a call
  from Hermes: granted only with `answer_sessions`, the call's chats, and
  bound to them. It answers a question with the user's words or the number
  of their choice (or "skip" for one of a form, 0.82+), and approves or denies only with `answer_approvals: true`
  (the Watch's "Tap or voice" setting), saying it went only once Hermes
  took it. On Local Live the host passes the user's words to it itself.
- **Jobs** start an ordinary Hermes chat on the grant's profile through
  Hermes' own session API in the dashboard process, filed under Voice Jobs in
  Conduit like a job started from the phone; `job_options` carries the
  phone's voice-job model, provider and reasoning effort. `max_jobs` is the
  user's per-call limit (default 5, at most 20, 0 for none), counting the
  jobs that started; three run at once. A grant with jobs allows 120 calls,
  since the Watch also asks for job news (finished jobs and approval
  requests) while they run. Each approval request carries a `fingerprint` of the
  request as shown; `answer_approval` sends it back, and is answered
  `changed` (the new request comes as news) if the request changed since.
  A request Hermes sent without a `request_id` can only be answered with
  the card's `fingerprint`, since Hermes then answers its oldest pending
  request. A start Hermes takes longer than 18 s to accept
  is answered `accepted`, inside the relay's wait, and a start that then
  fails comes as job news. News whose answer doesn't reach the relay is
  sent again with the next call. A renewal names the call's previous grant
  in `carry_jobs_from`: while that grant is open and on the same profile, its
  jobs move to the new one (`jobs_carried_from`), so a call keeps hearing
  about, listing and cancelling them past a renewal, and the job limit
  counts per call. A renewal that leaves out `max_jobs`, `job_options` or
  `job_profiles` keeps the call's.
- **The call's chat** (plugin 0.78): a `start_job` with `joins_call: true`
  is ordinary work, not asked for in the background, in a separate chat or
  quickly, and not for another profile (the Watch decides, as the phone's
  calls do). The call's first such job that starts makes its chat. Each later
  one is still a job of its own, with its own id and news, but runs as that
  chat's next turn, once nothing else runs there, so Hermes keeps the
  earlier requests. A request waiting its turn is answered `accepted` after
  18 s, can be cancelled without touching the running one, and is answered
  `not_started` if the call ends first. The chat closes after its last
  request settles once the call has ended. Without `joins_call`, or from an
  older Watch app, each job gets a chat of its own, as before.
- **Jobs on another profile** (plugin 0.11): `job_profiles` lists the user's
  other profiles (at most 32 names) a call's jobs may run on, as the phone's
  "for Fam, …" jobs do. A `start_job` with `profile` naming one of them runs
  there, on that profile's own model (`job_options` stays with the grant's
  profile) and filed under that profile's Voice Jobs; a name not listed is
  answered `not_started` and never guessed at. A grant with jobs answers
  `job_profiles` (the listed names, possibly none); an older plugin's
  answer leaves it out, and the Watch then sends such jobs through the iPhone.
- **Native tickets** (plugin 0.51.4): a request authorised by a Hermes
  native-http ticket is bound to the ticket's profile home. Its grant opens,
  and its revoke runs, only when the request's profile is that home (else
  403), keeps only
  `job_profiles` on that home (usually none), and carries a previous grant's
  jobs only while every profile they may name, and every job they ran on
  another profile, is on it too; otherwise `jobs_carried_from` is left out and
  the renewal starts with jobs of its own. A ticket whose home the plugin
  can't read is refused (403).
- **Follow-ups** (plugin 0.10): a grant that can start or cancel jobs also
  carries `interrupt_job` (`{job_id, message, kind?}`), as does one that names it. It puts the
  user's words into a job Hermes is still working on, through Hermes'
  `session.redirect` (a steer on a Hermes without it), as Conduit's phone
  calls do. With `kind: "add"` (plugin 0.65.1) the words only add to the
  work, so they go in as a `session.steer` instead, falling back to the
  redirect when Hermes refuses or lacks the steer: Hermes keeps the work so far and the job's result still comes as
  news. The answer is `{ok, outcome, title?, error?}`, the outcome being
  `interrupted`, `queued` (taken right after the step Hermes is finishing),
  `finished`, `failed` or `unknown_job`; follow-ups to one job go one at a
  time.
- **Gemini Live tokens** (plugin 0.8): with `live_token` in `tools`, and
  only where this profile has a Gemini key, the Watch can ask for a fresh
  single-use Gemini Live token, minted as `/gemini-live/token` mints one
  and answered with the same fields. It's for a call whose Gemini session
  broke and can't be resumed while the iPhone is out of reach. It travels
  sealed like every answer, so the relay never sees it, and the Gemini key
  never leaves this host. A grant mints at most 6, and a renewal, which only
  the iPhone can ask for, counts its own; a mint that fails, or whose answer
  never reaches the relay, doesn't count. A request for
  `live_token` alone on a profile without a key is refused (503).
- **Qwen Live tokens** (plugin 0.75): `qwen_live_token` does the same for a
  Qwen Live call, minted as `/qwen-live/token` mints one, only where this
  profile has Qwen Live set up. Each tool mints only its own provider's
  credential, and the two share the grant's 6. A request for
  `qwen_live_token` with nothing else grantable on a profile without Qwen
  Live is refused (503).
- **Chats** (plugin 0.18): `find_chat` (`{query}`) and `read_chat`
  (`{number}`) let a call find one of the user's earlier chats on the
  grant's profile and talk about it, as a phone call does. `find_chat` runs
  Hermes' own `GET /api/sessions/search` in process (a query that matches
  nothing as a whole is tried a word at a time) and answers `{count, chats}`,
  the numbered matches as fenced text for the voice model. `read_chat`
  reads only a chat the grant's newest search returned, by its number,
  through Hermes' own `GET /api/sessions/{id}/messages` (newest 500 rows,
  user and Hermes text only) and answers `{chat}` (`{chat: "", title}` for a
  chat with no messages): word for word when
  short, else a summary of the older part (a saved voice call's stored one,
  or one made once per call with Hermes' `compression` model) plus the
  latest turns; `{unknown: true}` for a number the search didn't return. A
  Hermes without the search route leaves both out of the grant, so the
  Watch asks the iPhone instead. Capability `watch-chats`.
- **Approvals** follow the profile's own Hermes approval settings. A command
  that needs one shows on the Watch, which can only approve it once or deny
  it, never for the session or always. Hermes denies it after its own
  approval timeout.
- Jobs need the dashboard to serve Hermes' chats in the same process (not
  `plugins.isolation: host`). Where it doesn't, the grant leaves jobs out and
  the Watch's jobs go through the iPhone. A job still running when its call
  ends keeps running in Hermes, and its chat is closed here once it finishes.
- 400 for a bad body, 409 when this profile isn't paired with Conduit
  notifications, 501 without the `cryptography` package or a relay that
  predates Watch tools, 502/503 when the relay can't open the grant (the call
  then uses the iPhone).

## Apple Watch voice: GPT-Live and Grok (plugin 0.9+)

Gemini Live runs on the Watch itself. GPT-Live on a ChatGPT subscription
speaks only WebRTC, which the Watch can't run, and Grok runs on this host's
xAI sign-in, which never leaves it. So for those two the host holds the
provider session and the Watch streams its audio to it through the push
relay. A Watch grant opened with `audio: true` returns
`audio: {url, version, engines}`. The plugin then dials the relay's gateway
side for that grant and waits, and the Watch dials `url` with the grant's
relay key. The host still opens no inbound route.

| Method | Route | Returns |
| --- | --- | --- |
| GET | `/api/plugins/conduit_push/watch-audio/status` | `{ok, version, engines: {gpt_live: {runtime, source, reason}, grok: {...}}}` |
| POST | `/api/plugins/conduit_push/watch-audio/prepare` | `{ok, version, engines: {gpt_live: {runtime, source, reason}}}` |

- **Messages** are sealed per Watch connection with a key from the grant's
  root, so the relay can neither read nor forge one. The Watch speaks each
  engine's own events, as the phone does, and its tools and jobs go through
  the grant's lookups. The wire format, keys and control messages are
  described where they're built (`dashboard/plugin_api.py`, "Watch audio").
- **GPT-Live** runs WebRTC in a small helper process
  (`dashboard/watch_audio_helper.py`) under a Python that has `aiortc`. The
  plugin does the SDP exchange with the Codex sign-in itself, so the
  sign-in never enters the helper. On any Hermes install, `prepare` makes the
  plugin's own environment for it: `python -m venv` from the Python Hermes
  runs, or `uv` where that Python has no venv or pip, then
  `pip install aiortc==1.15.0` (about 150 MB), run isolated and without
  Hermes' `PYTHONPATH` so it installs everything aiortc needs rather than
  counting on packages Hermes already has. It goes in
  `~/.hermes/conduit_push/watch-audio-env` (under `HERMES_HOME` when that's
  set, or `CONDUIT_WATCH_AUDIO_ENV`). Nothing is installed into Hermes' own
  Python; if that Python already has `aiortc`, it's used as it is.
  `runtime` reads `ready`, `preparing`, `failed` (with the reason) or
  `missing`, and `engines` in a grant lists `gpt_live` only once it's
  ready. The helper writes to the plugin from its own thread, so a slow
  Watch connection never stalls the WebRTC side: past 2 s of model audio
  waiting, the oldest is dropped, and events are never dropped.
- **Grok** connects to xAI the way the phone's Grok relay does. Grok sends
  a reply's audio faster than it plays, so the plugin paces it to the Watch
  at real time with a little lead, and drops what's left when the user
  speaks over it.
- A host holds at most 4 Watch calls at once, one per grant. 501 when the
  relay predates Watch audio (the grant is closed again) or this Python has
  no `websockets`.
- `probes/watch_audio_client.py` is a command-line stand-in for the Watch:
  it opens a grant with audio, calls through the real relay, plays a WAV
  question and records the answer. `--prepare` makes the GPT-Live runtime
  first.

## Rooms with other people's agents (plugin 0.79+)

A gateway-hosted room can seat an agent from another installation (a RoomLink
peer member, `target.kind: "peer"`), so agents of different owners talk in
one room (Linked Rooms, designs/cross-owner-agents). Each owner's rules for
their own agent run on their own host, through this plugin's hooks, whichever
host the room lives on. Rooms of only this host's own agents work as before.

The plugin tells a room turn apart by its session: an agent sitting in a room
hosted elsewhere runs in `room_<digest>`, and a local member runs in a hidden
`bot_room` session titled `Group: <room id>` whose room, in the room store
(this profile's `state.db`, the root's, the legacy `shared-state.db` or another
profile's), seats a peer member. A compaction's continuation keeps its room
(Hermes moves the session's title to it, and the plugin follows it there),
and so does a helper agent the room's session delegated to.

Each profile keeps its choices per room in `conduit-rooms.json`, plus defaults
for rooms with none of their own:

- **What it can do**: Talk only (no tools, the default), Look things up
  (`web_search`, `web_extract`, `x_search`, `vision_analyze`), Use my data
  (lookups plus the room-safe data tools you pick; there are none yet) or
  Custom (only the tools you pick). Always within the profile's own toolsets.
  Tool Search's own tools (`tool_search`, `tool_describe`, `tool_call`) work
  whenever anything is allowed: Hermes runs the hooks on the tool `tool_call`
  reaches, by that tool's own name, so it's checked like a direct call, and a
  `tool_call` the hooks see as itself is checked by the tools it names.
  Memory and skill writes, delegation, messaging, scheduled jobs, running code
  and Conduit's calls and settings (`memory`, `skill_manage`, `delegate_task`,
  `kanban_create`, `send_message`, `message_agent`, `react_to_message`,
  `cronjob`, `cronjob_manage`, `execute_code`, `conduit_call_user`,
  `conduit_call_watch`, `conduit_settings`) are never allowed, and that holds
  for the background memory and skill review too, since its calls carry the
  room session's id. `execute_code` is never allowed because its script calls
  tools without the room session's id, so these rules wouldn't see those
  calls. What you pick for Custom is yours to judge: a pick like
  `write_file`, or a memory provider's own tools, can still save what's said
  in the room.
- **Search my past chats**: off by default; `session_search` is blocked
  unless it's on.
- **What it may share**: what helps, ask you first (the default) or nothing
  personal. The agent is told, every room turn, along with who's asking (in a
  room hosted elsewhere, the room's "user" is that host's owner, not you),
  that it answers only for itself, and your private note for the room. The
  agents from elsewhere are named by their handles, or as "another agent"
  when a handle isn't shaped like Hermes' own (the other host chose it).
- **How it takes part** (joins in, only when asked, listens only) and **turns
  a day** (40 by default) are kept for the room bridge, which enforces them
  once it ships.

A room turn with other people's agents gets only that framing from the
plugin: none of its other notes (Conduit's data, voice hints, declined calls).
Its reply leaves the host with credential-shaped text (`sk-` API keys, Google
and xAI API keys, GitHub and Slack tokens, AWS key ids, JWTs, bearer tokens,
private key blocks) replaced by `[credential removed]`. In every chat,
`session_search` no longer finds the hidden sessions of rooms with other
people's agents, and a direct read of one answers "session_id not found".

If the room's roster or the choices can't be read, or aren't in a shape Hermes
or the plugin writes, that turn gets no tools, with a warning in the log (the
first time and every 100th), and so does a room session Hermes hasn't titled
with its room yet. If the session store itself can't be read, the plugin can't
tell a room's turn from another chat's, so it blocks each tool call it can't
check, in any chat, with a note to the agent to try again in a moment, and logs
a warning; a turn on Hermes' room platform still gets the room's framing and
has its reply checked. A room that no room store has (kept where the plugin
doesn't look) runs as Hermes alone would run it, with a warning: its tools, and
its sessions in past-chat search (a `room_<digest>` session still counts as a
room). So does a turn on a host where the plugin can't find the Hermes home,
with a warning (the first time and every 100th): blocking there would block
every chat on that host. A disbanded room's sessions that stay (a member host
keeps its agent's, below) keep its rules: they hold what other people's agents
said. A `session_search` result that mentions group rooms in a shape the
plugin can't read, from a profile whose name isn't of the shape Hermes gives
one, or with no Hermes home to check it in, goes to the agent as a failed
search. A Hermes without `transform_llm_output` or `transform_tool_result`
logs a warning, and replies or searches then go out unchecked.

These rules work through Hermes' tool hooks, so they hold only where those
run. A profile on the Codex app-server runtime runs Codex's own tools outside
them: don't put one in a room with other people's agents (a later release
refuses it there).

```bash
hermes conduit-push rooms show                  # defaults, then each room
hermes conduit-push rooms set --room <room id> --tools look --search on
hermes conduit-push rooms set --room <room id> --tools custom --tool terminal --tool read_file
hermes conduit-push rooms set --share nothing --limit 20   # the defaults
```

Conduit reads and changes them through `GET /rooms/choices?room_id=<id>`
(`saved: false` when the room uses the defaults; without `room_id`, every
room), `PUT /rooms/choices` with `{"room_id": ..., "choices": {...}}` (only
the choices it names change), and `GET`/`PUT /rooms/defaults` with
`{"choices": {...}}`, all with `?profile=`, behind the `room-rules`
capability.

## A member cut off from a room (plugin 0.81+)

When a room's home sends a turn to a member on another installation and that
host refuses it because the room's grant was revoked or has run out, Hermes
records the member's route as needing reauthorization and puts the turn back
in the queue, trying again every 30 s at most, forever, so the room waits on
that member. The plugin fails that member's next queued turn before it's sent,
with the reason "cut off: <member>'s Hermes no longer accepts this room" (a
`turn.failed` in the room's log, with reason code `unknown`: Hermes' failure
codes have none for this), the way Hermes fails a turn whose member has no
destination, so the discussion goes on without them. Hermes marks the route
ready again each time it sets up a retry, so the plugin reads the route before
Hermes sets the turn up. Linking the member again stores its route as ready,
and its turns go through again. A member that's only offline is retried as
Hermes retries it, and a turn being stopped or recovered goes to Hermes as
before. A Hermes whose room driver has no `_transport_for` logs one line and
keeps its own behaviour.

## Disbanding a room ends its hidden chats (plugin 0.81+)

Each agent in a gateway-hosted room keeps a hidden session for it (source
`bot_room`, titled `Group: <room id>`), and Hermes keeps those after the room
is disbanded, though Conduit's warning says disbanding ends them. When a room
this host hosts is disbanded (from Conduit, through either room service's
`groups.disband`), the plugin deletes this host's own agents' hidden sessions
for it, in every room, rooms of only your own agents too (Eric, 2026-10-26):
each with its compressed history and its helpers' chats, the way Hermes' own
delete takes one. A branch made from one stays, as a chat of its own. The
disband answers before the delete is done, and never fails because of it. A
session a turn is still finishing in, or one Hermes' delete fails on, is tried
again 30 s later, then a minute after the plugin next loads (Hermes starts, or
reloads the plugin), which also deletes what rooms disbanded before this
version left behind (the newest 512 disbanded rooms of each room store; one
it can't read is logged as a warning and read again at the next load). An
agent placed by `hosted_rooms.profiles` is found in the home that names. A room
whose members the plugin can't read (its store was locked as it was disbanded,
or its roster is damaged) is looked for in every agent's home on this host.
Agents on other people's Hermes keep their sessions for the room: someone who
leaves keeps their agent's session, and a revoke can't tell leaving from
disbanding. A Hermes without `disband_room`, or whose session delete can't
take a compression chain, logs one line and deletes nothing.

Your agent's hidden session for a room hosted elsewhere stays until you
delete it:

```bash
hermes conduit-push rooms forget --room <room id>         # says what it deletes, then asks
hermes conduit-push rooms forget --room <room id> --yes   # without asking
```

It deletes this profile's hidden sessions for that room, with the same delete,
whichever side they're on. If the room is still going on someone else's
Hermes, your agent starts over there in a fresh session on its next turn. A
room hosted here and still going is refused: disbanding it ends its chats.
Conduit does the same through `DELETE /rooms/sessions?room_id=<id>&profile=`,
behind the `room-forget` capability: `{"ok": true, "room_id": ..., "deleted":
1, "kept": 0}`, where `kept` counts sessions a turn is still finishing in (try
again in a minute), with 409 for a room still going here, 503 when Hermes'
session store is busy or won't open, or its delete fails on one (try again
shortly), 501 on a Hermes whose session delete can't take a compression chain
(update Hermes), and 400 for anything that isn't a room id.

## Linking with someone (plugin 0.83+)

Before one of your agents can sit in a room with someone else's, your Hermes
and theirs link: each keeps the other's keys, and each can leave sealed
envelopes in the other's inbox at the rooms mailbox (above). A Link belongs to
the installation, so every profile shares it, as it shares the keys it's made
with (`conduit-rooms/identity.json` in the root Hermes home, made at the first
invite or accept). Nothing goes over a Link yet: the room bridge, in a later
release, carries rooms over it.

One of you makes an invite: a `conduit://link/` URL, also the QR code's
payload, good for one accept within 24 hours. The other accepts it, giving the
name you'll see. Their Hermes leaves the accept in yours, yours answers with a
confirm, and you're linked. An invite shows four fingerprint words for each
Hermes' keys: when you're together, check you both see the same four, so the
invite is from who you think. The confirm, signed by the inviter's Hermes,
names its mailbox, and the acceptor's Hermes uses that one from then on, so an
invite whose mailbox was changed on its way can't send the Link elsewhere.

```bash
hermes conduit-push links invite --name Eric             # the invite, and your Hermes' words
hermes conduit-push links preview <conduit://link/...>   # who it's from, their words and mailbox
hermes conduit-push links accept <conduit://link/...> --name Mona
hermes conduit-push links list                           # Links, invites still waiting, the mailbox
hermes conduit-push links remove <Link id>               # unlinks, on both sides; asks first (--yes doesn't)
hermes conduit-push links cancel <invite id>             # takes an invite back
```

A Link waits until the inviter's Hermes confirms it, and one still waiting
after 8 days reads as unanswered (its accept or confirm ran out at the
mailbox). An accept the mailbox didn't answer, or couldn't take yet, stays
with its Link and is tried again while the invite lasts; accepting the same
invite again is the same accept. Unlinking ends a Link here at once, and the other
Hermes drops it when it next reads its inbox; one unlinked while it waits for
its confirm is answered with the unlink when the confirm comes. A confirm that
runs out at the mailbox before the acceptor's Hermes reads it (off for more
than 7 days) leaves the inviter's side linked until its owner unlinks it. An
unlink, accept or confirm the mailbox can't take now goes later, up to its
7-day hold (an accept, while its invite lasts), and one it refuses as it is
(too large, say) doesn't. An invite that says it lasts more than two days
isn't taken: the clock where it was made is wrong, or someone made it by
hand. To link again, unlink first: a new accept from the same Hermes
then replaces the inviter's old Link, as it does after someone loses their
side. If you and they accept each other's invites before either Hermes reads
the other's accept, you're linked twice, the same on both sides; unlink
either. This Hermes keeps up to 50 Links, each invite still waiting holding a
place for one, and up to 10 waiting invites. An invite that ran out, and a
Link unlinked while it waited, keep their place until Hermes' dashboard has
read the mailbox for them, and a refusal for the count says how many. Everything a
Link needs, its send tokens included, stays in `conduit-rooms/links.json`
(owner only), never in a log or a route's answer.

The dashboard's process reads the inbox: one loop for the installation,
whichever process holds `conduit-rooms/.lease`. While nothing waits it only
looks at `links.json` every 2 s, and it reads the mailbox once there's an
invite, a Link or something to send. A command run while no dashboard runs
makes its change, and says the answer waits at the mailbox, for up to 7 days,
until the dashboard runs with this plugin (an accept the mailbox didn't take
yet waits on this Hermes instead).

The mailbox is rooms.milim.dev unless `CONDUIT_ROOMS_MAILBOX_URL` names
another (`https://`, with nothing after its host name, or `http://` on this
computer for a test; see [`mailbox/README.md`](mailbox/README.md) to run your
own), and `CONDUIT_ROOMS_MAILBOX_REGISTRATION` holds the registration token
one that asks for it gave you. Every Link and invite names the mailbox it was
made with, so while any is left the plugin keeps that one, and logs that a new
setting takes effect once none is.

Conduit makes and reads them through these routes, behind the `room-links`
capability, with no `?profile=` (Links are the installation's):

- `GET /rooms/identity`: `{"words": [...], "crypto": true}`, with `words`
  null until the first invite or accept, and `crypto` false on a Hermes
  without Python's cryptography package (which refuses with `no_crypto` once
  it has keys).
- `POST /rooms/links/invites` with `{"name": ...}`: `{"url": ..., "invite":
  {"id", "name", "expires_at"}, "words": [...]}`, with this Hermes' words.
- `POST /rooms/links/preview` with `{"url": ...}`: `{"name", "words",
  "mailbox", "expires_at", "expired", "own", "linked_as"}`, where `mailbox`
  is the host of the mailbox the invite names and `linked_as` the Link this
  Hermes already has with them, or null.
- `POST /rooms/links` with `{"url": ..., "name": ...}`: `{"link": {"id",
  "name", "status", "words", "created_at"}}`, its status `waiting`. A 503
  with one of the mailbox's codes says it didn't take the accept yet: the
  Link stays, waiting, and its accept is tried again while the invite lasts.
- `GET /rooms/links`: `{"links": [...], "invites": [...], "mailbox": {"url",
  "reading"}}`, statuses `waiting`, `linked` or `unanswered`, and `reading`
  false while nothing on this Hermes reads the mailbox.
- `DELETE /rooms/links?link_id=`: `{"removed": true}`, and `DELETE
  /rooms/links/invites?invite_id=`: `{"cancelled": true}` (false for one this
  Hermes doesn't have).

A refusal carries `detail` and a `code`: `bad_name` and `bad_invite` (400,
also for an invite naming a mailbox on someone's own computer or a private
network address, which this Hermes calls only when it's its own mailbox
too), `own_invite` (400, the invite is this Hermes'), `expired` and `invite_used`
(410), `already_linked`, `too_many_links` and `too_many_invites` (409),
`no_crypto` and `no_word_list` (503, a plugin install missing its word list),
and the mailbox's own, such as `clock_off` (503) when this host's clock is far
from the mailbox's. A mailbox that can't be reached is 503, one that answers
what it shouldn't is 502. A route that waits on the mailbox answers within
50 s, or with `timed_out` (504) while the call finishes on its own. The routes
answer 60 requests a minute between them, one still waiting for a thread
included.

## Capabilities (plugin 0.4+)

`GET /api/plugins/conduit_push/capabilities` returns the plugin version and
the Conduit features its routes serve:

```json
{"ok": true, "version": "0.24.1", "loaded_version": "0.24.1", "restart_needed": false, "boot_id": "9f2c41d07a3b5e68", "capabilities": ["gemini-live", "web-search", "...", "pairing", "self-restart"]}
```

Conduit reads it on connect. When a feature it uses is missing, or the route
itself is (a plugin older than 0.4), Settings asks you to run
`hermes plugins update conduit_push` and restart the gateway. Every new route
family adds its name here.

## Pairing from Conduit (plugin 0.24+)

`POST /api/plugins/conduit_push/pair?profile=` with
`{"pairing_code": "ABCDE-FGHJK", "relay_url": "https://push.milim.dev"}`
makes the same claim as `hermes conduit-push pair` for that profile and saves
the credential in its `conduit-push.json`. `relay_url` is optional (the
profile's current relay, else push.milim.dev) and must be a bare http(s) URL;
the claim carries only the code and the gateway name, never follows redirects,
and the answer never includes the credential:
`{ok, paired, installation_id, gateway_id, gateway_name, relay_url}`. A
re-pair keeps the profile's redaction choice; it keeps the encryption key when
the same phone and gateway pair again, and otherwise drops it (Conduit
provisions a new one). 400 for a malformed code or URL, 410 when the
code expired or was used, 429 past 10 requests a minute, 502 when the relay
can't be reached.

## Updating from Conduit (plugin 0.24+)

Conduit updates the plugin with Hermes' own dashboard routes
(`POST /api/dashboard/agent-plugins/install` and
`POST /api/dashboard/agent-plugins/conduit_push/update`), then restarts the
gateway (`POST /api/gateway/restart`), then this dashboard:

- `GET /capabilities` also reports `loaded_version` (the version this
  dashboard process imported), `restart_needed` (the version on disk is
  different) and `boot_id` (new with every dashboard process).
- `POST /api/plugins/conduit_push/self/restart` answers
  `{ok, restarting, boot_id, loaded_version, version}` and a second later
  re-executes the dashboard with the command line it started with, keeping its
  pid, so systemd, launchd and terminal launches carry on. 409 while a chat
  turn this process runs is still going (from Hermes' turn markers); 501 on
  Windows, inside a plugin host, and for Hermes Desktop's own backend, where
  you restart Hermes yourself.

The first update to 0.24 still runs the old code, which has no restart route:
restart the dashboard by hand once.

From 0.63 the host can run the whole update itself, so Conduit can be closed
while it runs:

- `POST /api/plugins/conduit_push/self/update?profile=<name>` starts it and
  answers `{ok, job}`: Hermes pulls the plugin, the gateway restarts, and the
  dashboard re-executes once no chat turn runs here. A second call while one
  runs answers the same job. 501 where `/self/restart` would, or when Hermes
  lacks its own pull and gateway restart calls; Conduit then runs the steps
  above itself. 429 after three starts in ten minutes.
- `GET /api/plugins/conduit_push/self/update` answers `{ok, job, boot_id,
  loaded_version}`. `job.step` is `downloading`, `restarting_gateway`,
  `waiting_for_chats` (with `running_chats`), `restarting_dashboard`, `done`
  or `failed` (with `detail`, `failed_step` naming the step it failed in,
  and `code` for `consent_required`, `scan_blocked` or `restart_needed`).
  Chats still running after two hours fail the job with `restart_needed`:
  the new plugin is on disk and loads on the next dashboard restart.
- The job is kept in `conduit-self-update.json` in the Hermes root. The
  restarted dashboard marks it done and sends the profile that asked a
  notification saying which version is running, or why the update didn't
  finish.

Every version is listed in [CHANGELOG.md](CHANGELOG.md), which Conduit's update
notice reads.

## Conduit support and privacy

The repository also hosts the public Hermes Conduit support and privacy pages:

- [Hermes Conduit support](https://kaishi00.github.io/hermes-conduit-notifier/support/)
- [Hermes Conduit privacy policy](https://kaishi00.github.io/hermes-conduit-notifier/privacy/)

The static site source lives in [`docs/`](docs/).

## Development

The runtime uses only the Python standard library plus Hermes APIs. CI ([`.github/workflows/tests.yml`](.github/workflows/tests.yml)) runs the plugin, relay and mailbox tests on every PR and every push to main. To run them locally:

```bash
pip install -r tests/requirements.txt
python -m pytest --rootdir=tests tests
(cd relay && npm test)
(cd mailbox && npm test)
```

Working on a feature with siblings in the app (a voice mode, the Watch, CarPlay)? See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT
