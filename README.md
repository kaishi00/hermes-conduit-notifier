# Hermes Conduit Notifier

Hermes Conduit Notifier is the open-source Hermes plugin that delivers lifecycle notifications to the Hermes Conduit iOS app. It observes normal Hermes hooks and sends small HTTPS events to the Conduit push relay.

The plugin does **not** contain an Apple Push Notification service key, dashboard credentials, or access to your Hermes gateway. (Its only dashboard routes hand Conduit short-lived Gemini Live tokens, start GPT-Live sessions on the host's ChatGPT subscription, relay Grok Live calls with the host's SuperGrok sign-in, run its voice web lookups, read memory and personality for voice, save live voice transcripts to this host's session history, ending each call's session there so session hooks and memory see it, and open short-lived grants that let an Apple Watch call run lookups and Hermes jobs through the relay; see below.) Apple credentials remain on the central push relay, so self-hosted users never need to copy a shared signing key onto their gateway.

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

## Events

The plugin currently emits notifications for:

- approval needed
- clarification or other input needed
- response ready
- failed turns
- completed delegated tasks
- call requests: a job you asked to be called about has ended, Hermes asked to call you, or (alert calls) Hermes has waited a minute on your approval or answer, or a turn failed (see [Hermes calls you](#hermes-calls-you-plugin-013))

An exact `[Silent]` assistant response does not emit a completion notification.

The iOS app controls which categories are enabled, whether notification previews are shown, whether completion sounds play, and whether approval and input-needed notifications play a sound (on by default).

## Privacy and security

- Pairing creates a revocable, profile-scoped credential.
- The credential is stored in the profile-aware Hermes home as `conduit-push.json` with mode `0600` on supported systems.
- Authorization credentials are never written to logs or printed by `status`.
- Hook callbacks enqueue bounded events; HTTPS delivery runs on a background worker and does not block the agent loop.
- Event titles and bodies are length-limited before delivery.
- Lock Screen previews are disabled by default in Hermes Conduit.
- **End-to-end encryption** (plugin 0.5+, relay 0.5+, a Conduit build with encrypted notifications): Conduit creates a secret for each pairing on the phone and hands it to this profile over your Hermes dashboard connection, so it never passes through the relay. From then on every notification's title, body, profile, session and decision card is sealed with it (ChaCha20-Poly1305, keys derived with HKDF-SHA256), the phone decrypts it in a Notification Service Extension, and clarify answers come back sealed for this profile. The relay and APNs see only routing data: the event type, opaque ids, a keyed thread token and timing. A pairing that has a key never sends plaintext content again, and only accepts clarify answers sealed by the phone. `hermes conduit-push status` shows whether it's on; Conduit turns it on by itself when it connects to this host. Run `hermes conduit-push test --corrupt` to check that the phone shows only generic text for a push it can't verify. The secret is only as private in transit as your dashboard connection (HTTPS or a private tunnel is recommended).
- `hermes conduit-push redact on` keeps chat content from leaving the gateway: events carry no title or body text, approval cards say "Hermes needs your approval. Open Conduit for details.", and clarify cards replace question text with generic copy. The fields needed to answer stay (session key or request id, question ids, choice labels), so cards raised while the app is backgrounded remain recoverable and answerable. Clarify choice labels still transit the relay because the answer is one of them. `status` shows the current setting; `redact off` restores full content, including for events still waiting in the local delivery queue.

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
maintenance; the relay never deletes installation records or their
credentials automatically. `DELETE /v1/installations/:id` deactivates an
installation and does not free its slot.

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
| POST | `/v1/installations` | Register a device |
| PUT | `/v1/installations/:id` | Update device token, PushKit token (`voip_token`, `null` clears it), the paired Apple Watch's PushKit token (`watch_voip_token`, 0.10+) / preferences |
| DELETE | `/v1/installations/:id` | Deactivate a device |
| POST | `/v1/installations/:id/pairings` | Create a pairing code |
| POST | `/v1/pairings/claim` | Claim a pairing code (gateway side) |
| POST | `/v1/events` | Deliver a notification event |
| DELETE | `/v1/gateways/current` | Revoke a gateway credential |
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

Watch tool grants carry an Apple Watch call's lookups while the Watch can't
reach Conduit on the iPhone. Calls and answers are sealed with a per-call key
the relay never sees (ChaCha20-Poly1305, bound to the grant, call id and
direction), so the relay only forwards ciphertext. Grants live in memory,
last at most 30 minutes and 120 calls, and a restart drops them; the Watch
then falls back to the iPhone. A relay holding its maximum of grants answers a
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

Event IDs only prevent a repeated delivery of the same event within 24 hours,
so the relay keeps them in memory and never writes them to the data file. A
restart forgets them, which costs at most one duplicate push; replaying an
event after a restart needs that gateway's credential, which can send new
events anyway. An installation
at its event-ID bound forgets its own oldest ID, and at the relay-wide bound
the installation holding the most IDs forgets its oldest, so neither bound
rejects an event. A gateway's last-seen time and plugin version are written
at most every five seconds, and on shutdown, instead of on every event. Each
of those writes is still a full rewrite of the data file, so a busy relay
spends one whole-file write per five seconds on them rather than two per
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
- Duplicate qid answers and released decisions are distinct outcomes:
  `409 already_answered` settles only that qid as answered elsewhere,
  while `410 decision_released` (the native Desktop/TUI path resolved the
  whole clarify) tells Conduit to retire the entire pushed card. An
  unknown qid on a live decision is `400 invalid_question_id`, not a
  missing decision.
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
event ID. Retrying is caller-chosen: the shipped plugin logs the rejection and
does not retry automatically; a caller may explicitly retry the same event ID
after capacity becomes available and before the event is accepted. Hermes'
native clarify path remains available, and existing decisions remain
answerable, pollable, and cancellable.

The global limits apply across every installation. Because installation
self-registration is open, one actor can create multiple installations,
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

## GPT-Live on a ChatGPT subscription

This is the host half of [hermes-agent#108940](https://github.com/NousResearch/hermes-agent/pull/108940),
shipped in the plugin so Conduit doesn't wait on the upstream merge. It lets a
GPT-Live voice session bill your ChatGPT/Codex subscription instead of an OpenAI
API key. First sign in on the Hermes host with `hermes auth` and choose OpenAI Codex.

| Method | Path | Returns |
|--------|------|---------|
| GET | `/api/plugins/conduit_push/gpt-live/status` | `{ok, auth, available, reason, model, voice, source}` |
| POST | `/api/plugins/conduit_push/gpt-live/session` | `{ok, auth, session: {id}, transport: {type, sdp}, source, voice, briefing_applied, greeting_applied}` |

The session route takes `{"sdp": "<WebRTC offer>", "history": [...], "voice": "cove", "briefing": "..."}` (history
optional, last 40 items kept; voice optional and, when given, used instead of `subscription_voice`; briefing optional, up to 32 KB, and added to the session instructions so the model has Conduit's rules before it speaks; greeting optional: absent, the model stays silent until the user speaks; `""` or text, it greets the user first, saying the text when given, up to 200 characters) and returns the SDP answer plus the `voice` it used. The plugin posts the
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
change. `voice.gpt_live.auth` is Hermes desktop's setting; these routes always
use the subscription.

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

## Voice call transcripts

Gemini Live, GPT-Live and Grok Live calls never run a Hermes turn, so nothing records
them. These routes let Conduit save a call's transcript as an ordinary session
in this profile's history, written straight into the session store. They never
start the agent or call a model. A resumed call appends to the same session.

| Method | Route | Returns |
| --- | --- | --- |
| POST | `/api/plugins/conduit_push/voice/sessions` with `{call_id, engine, session_id?, title?, turns: [{index, role, text, at?}]}` | `{ok, session_id, written, appended, created}` |
| GET | `/api/plugins/conduit_push/voice/tags` | `{ok, tags: {session_id: {kind, engine?, parent_id?, parent_title?}}}` |
| POST | `/api/plugins/conduit_push/voice/tags` with `{session_id, kind: classic or job, parent_id?, parent_title?}` | `{ok, session_id, kind, ...}` |
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
  `busy` (the other app is running a turn on it; ask again shortly, nothing
  is interrupted; a marker that can't be read counts as running, and only a marker whose writer is provably dead, or a gone owner's marker that names no writer, is ignored) or
  `same_host` (this dashboard process still holds it, for example Conduit on
  another device; that claim is left alone).
- The other app writes its turn marker without the registry lock, so a turn
  it starts in the instant between the check and the takeover still loses
  its claim. That turn keeps running there; nothing is interrupted.
- Only another process's claim is dropped; a claim without a valid pid is
  kept, since it can't be attributed. The other app isn't told: if you
  go back and send from Hermes Desktop, it still believes it owns the chat
  and doesn't see what you sent from Conduit until you reopen the chat there.
- `session_ids` takes 1 to 4 ids of one chat (its stored id and its live id),
  never ids of different chats.
  400 for a bad body, 501 on a Hermes without the ownership registry or turn markers, 503
  when the registry can't be read (ownership is never guessed), 429 past 60
  requests a minute, 504 if the takeover doesn't finish within 20 seconds (ask again: nothing is written after that, short of a write already under way).

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

Calls are off until you turn them on in Conduit's Voice settings. Settings
and watches live per profile in `<hermes home>/conduit-calls.json` (mode
`0600`), shared by the turn-end hooks and the dashboard routes under one file
lock, so they survive restarts:

| Setting | Default | Range |
| --- | --- | --- |
| `enabled` (Hermes can call me) | off | |
| `when_asked` (call when I explicitly ask) | on | |
| `decides` (Hermes decides when to call, 0.14+) | off | |
| `alerts` (approval, question and failure calls, 0.14+) | off | |
| `min_gap_s` (time between calls) | 120 s | 30–3600 s |
| `per_hour` | 6 | 1–30 |
| `per_day` | 20 | 1–60 |

| Method | Route | Body | Returns |
| --- | --- | --- | --- |
| GET | `/api/plugins/conduit_push/calls?profile=` | | `{ok, paired, settings, bounds, watches}` |
| PUT | `/api/plugins/conduit_push/calls?profile=` | `{settings: {...}}` (any subset) | `{ok, settings}` |
| POST | `/api/plugins/conduit_push/calls/watches?profile=` | `{session_ids: [...], title, hold_s?, ended_within_s?}` | `{ok, status: "watching", id}` or `{ok, status: "ended", outcome}` |
| PUT | `/api/plugins/conduit_push/calls/watches/{id}?profile=` | `{hold_s}` (0 releases) | `{ok, status: "watching"}`, `{ok, status: "ended", outcome}` or `{ok, status: "gone"}` |
| DELETE | `/api/plugins/conduit_push/calls/watches/{id}?profile=` | | `{ok, removed}` |
| PUT | `/api/plugins/conduit_push/calls/presence?profile=` (0.14+) | `{hold_s}` (0 releases) | `{ok, status: "present"}` or `{ok, status: "away"}` |
| POST | `/api/plugins/conduit_push/calls/outcomes?profile=` (0.15+) | `{call_id, session_ids: [...], outcome: "declined" or "missed", kind?, title?, reason?, age_s?}` | `{ok, status: "recorded"}` |

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
  failing (5xx) after three tries (2 s and 4 s apart, same event id). One
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
- The tool shows only while this profile is paired and one of those is on.
  Subagents can't call; their parent can.
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

## Apple Watch lookups and jobs (plugin 0.6+, jobs 0.7+, Gemini tokens 0.8+)

With the wrist down, a Conduit Watch call can't reach Conduit on the iPhone,
but it can reach the push relay over its own internet. For each call Conduit
asks this profile for a grant; the plugin opens it on the relay (see Relay API
above), long-polls the relay for the Watch's sealed calls, runs them here and
seals the answers back. This host opens no inbound route of its own.

| Method | Route | Returns |
| --- | --- | --- |
| POST | `/api/plugins/conduit_push/watch-tools/grant` with `{tools, max_jobs?, job_options?, job_profiles?, carry_jobs_from?, audio?}` | `{ok, grant_id, relay_url, key, watch_key, expires_at, tools, max_calls, max_jobs, job_profiles?, jobs_carried_from?, audio?}` |
| POST | `/api/plugins/conduit_push/watch-tools/revoke` with `{grant_id}` | `{ok, revoked}` |

- `tools` names what the call may run: `web_search` and `recall_memory`
  (the same code as the voice lookup routes), and with plugin 0.7
  `start_job`, `list_jobs` and `cancel_job` (granted with the Watch app's
  own `job_news` and `answer_approval`). A grant lasts at most 30
  minutes, holds one profile, and ends when the call does.
- **Jobs** start an ordinary Hermes chat on the grant's profile through
  Hermes' own session API in the dashboard process, filed under Voice Jobs in
  Conduit like a job started from the phone; `job_options` carries the
  phone's voice-job model, provider and reasoning effort. `max_jobs` is the
  user's per-call limit (default 5, at most 20, 0 for none), counting the
  jobs that started; three run at once. A grant with jobs allows 120 calls,
  since the Watch also asks for job news (finished jobs and approval
  requests) while they run. A start Hermes takes longer than 18 s to accept
  is answered `accepted`, inside the relay's wait, and a start that then
  fails comes as job news. News whose answer doesn't reach the relay is
  sent again with the next call. A renewal names the call's previous grant
  in `carry_jobs_from`: while that grant is open and on the same profile, its
  jobs move to the new one (`jobs_carried_from`), so a call keeps hearing
  about, listing and cancelling them past a renewal, and the job limit
  counts per call. A renewal that leaves out `max_jobs`, `job_options` or
  `job_profiles` keeps the call's.
- **Jobs on another profile** (plugin 0.11): `job_profiles` lists the user's
  other profiles (at most 32 names) a call's jobs may run on, as the phone's
  "for Fam, …" jobs do. A `start_job` with `profile` naming one of them runs
  there, on that profile's own model (`job_options` stays with the grant's
  profile) and filed under that profile's Voice Jobs; a name not listed is
  answered `not_started` and never guessed at. A grant with jobs answers
  `job_profiles` (the listed names, possibly none); an older plugin's
  answer leaves it out, and the Watch then sends such jobs through the iPhone.
- **Follow-ups** (plugin 0.10): a grant that can start or cancel jobs also
  carries `interrupt_job` (`{job_id, message}`), as does one that names it. It puts the
  user's words into a job Hermes is still working on, through Hermes'
  `session.redirect` (a steer on a Hermes without it), as Conduit's phone
  calls do: Hermes keeps the work so far and the job's result still comes as
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
  `pip install aiortc==1.15.0` (about 150 MB). It goes in
  `~/.hermes/conduit_push/watch-audio-env` (under `HERMES_HOME` when that's
  set, or `CONDUIT_WATCH_AUDIO_ENV`). Nothing is installed into Hermes' own
  Python; if that Python already has `aiortc`, it's used as it is.
  `runtime` reads `ready`, `preparing`, `failed` (with the reason) or
  `missing`, and `engines` in a grant lists `gpt_live` only once it's
  ready.
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

## Capabilities (plugin 0.4+)

`GET /api/plugins/conduit_push/capabilities` returns the plugin version and
the Conduit features its routes serve:

```json
{"ok": true, "version": "0.4.0", "capabilities": ["gemini-live", "web-search", "memory", "personality", "gpt-live", "grok-live", "voice-sessions", "voice-tags", "voice-summary", "session-takeover"]}
```

Conduit reads it on connect. When a feature it uses is missing, or the route
itself is (a plugin older than 0.4), Settings asks you to run
`hermes plugins update conduit_push` and restart the gateway. Every new route
family adds its name here.

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
