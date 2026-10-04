# Hermes Conduit Notifier

Hermes Conduit Notifier is the open-source Hermes plugin that delivers lifecycle notifications to the Hermes Conduit iOS app. It observes normal Hermes hooks and sends small HTTPS events to the Conduit push relay.

The plugin does **not** contain an Apple Push Notification service key, dashboard credentials, or access to your Hermes gateway. (Its only dashboard routes hand Conduit short-lived Gemini Live tokens, start GPT-Live sessions on the host's ChatGPT subscription, relay Grok Live calls with the host's SuperGrok sign-in, run its voice web lookups, read memory and personality for voice, and save live voice transcripts to this host's session history; see below.) Apple credentials remain on the central push relay, so self-hosted users never need to copy a shared signing key onto their gateway.

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

An exact `[Silent]` assistant response does not emit a completion notification.

The iOS app controls which categories are enabled, whether notification previews are shown, whether completion sounds play, and whether approval and input-needed notifications play a sound (on by default).

## Privacy and security

- Pairing creates a revocable, profile-scoped credential.
- The credential is stored in the profile-aware Hermes home as `conduit-push.json` with mode `0600` on supported systems.
- Authorization credentials are never written to logs or printed by `status`.
- Hook callbacks enqueue bounded events; HTTPS delivery runs on a background worker and does not block the agent loop.
- Event titles and bodies are length-limited before delivery.
- Lock Screen previews are disabled by default in Hermes Conduit.
- `hermes conduit-push redact on` keeps chat content from leaving the gateway: events carry no title or body text, approval cards say "Hermes needs your approval. Open Conduit for details.", and clarify cards replace question text with generic copy. The fields needed to answer stay (session key or request id, question ids, choice labels), so cards raised while the app is backgrounded remain recoverable and answerable. Clarify choice labels still transit the relay because the answer is one of them. `status` shows the current setting; `redact off` restores full content, including for events still waiting in the local delivery queue.

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

The relay bounds persistent admission at 1,024 installations, 16 gateways per
installation, 512 retained event IDs per installation, and 8,192 event IDs
across the whole relay (event IDs expire after 24 hours). New records are
rejected with HTTP 429 when a bound is full; the relay does not evict existing
credentials or event owners. An installation already above a limit when
upgraded keeps all its current records, but cannot add records of that kind
until the operator performs reviewed data maintenance while the relay is
stopped, preserving active credentials and unexpired event IDs.

Public registration has a process-wide admission limit of 24 requests per
minute. Authenticated device updates, pairing creation/valid claims, and event
intake share a separate 96-request-per-minute budget. Together these reserve
120 mutation admissions per minute without letting public registration drain
event capacity. Existing per-installation and per-client limits still apply;
validation and capacity rejection happen before shared budget charging.
Unchanged device updates and already-accepted events without plugin metadata
do not consume the ingress budget or rewrite the store. A duplicate carrying
plugin metadata still records that gateway's plugin state and uses the budget.

Decision answers and releases share a separate process-wide budget of 96
actions per minute. Installation deactivation and gateway revocation share
another 24-action-per-minute budget, so revocation traffic cannot exhaust the
answer/release quota. These are admission limits, not literal file-write
counts: one event can make up to five full-store saves while recording plugin
state, parking a decision, and handling an APNs failure. Health checks and
decision polling consume none of these four budgets. Device updates remain
limited to 30 requests per minute per installation, including unchanged
updates. Repeated decision cancellation does not rewrite the store or use
the shared decision budget, though its per-gateway limit still applies.

Run exactly one relay process/replica per `DATA_PATH`. The relay caches its
state and rewrites the whole JSON file; atomic replacement does not coordinate
multiple writers, and sharing the file between processes can lose updates.
The budgets are per process. Finite storage and request quotas remain
saturable and do not guarantee fair admission under a hostile flood.

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
256 across the relay. A scalar decision is active until answered or cancelled;
a batch remains active until every question is answered or the decision is
cancelled. Completed and cancelled decisions preserve their answers and locks
for the two-hour decision TTL, within separate retained-record caps of 128 per
installation and 1024 across the relay. Active decisions count toward both
retained caps. Since settled records stay for two hours, those retained caps
also impose a maximum admission throughput of 64 records per installation per
hour and 512 records per hour across the relay, averaged over a full retention
window.

When any limit is full, a new clarify event receives HTTP `429`
`decision_capacity_exceeded`. The relay sends no push and does not consume the
event ID, so the event can be retried after capacity becomes available.
Existing decisions remain answerable, pollable, and cancellable while the
relay is at capacity. The shipped plugin logs the 429 rejection and keeps
Hermes' native clarify path available; it does not automatically retry a
rejected event. Before acceptance, the same event ID may be explicitly retried
after capacity becomes available.

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
sense that the plugin issues no write calls: no `sync_turn`, no memory tool, so
the voice conversation is never recorded to memory. A provider may still keep
its own caches or bookkeeping while it recalls.

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
