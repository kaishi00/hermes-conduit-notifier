# Rooms mailbox

The rooms mailbox lets two Hermes installations on different networks pass each other sealed messages for a linked room, without either one opening an inbound port. Each installation owns an inbox and reads it with outbound requests. A linked installation leaves envelopes there with a send token the owner gave it.

The mailbox is blind. The Conduit plugin seals each envelope to the receiving installation's key and signs it before sending, so the mailbox only ever holds opaque bytes. From plugin 0.83 the plugin links installations through it (Linking with someone, in the plugin's README), and rooms go through those Links with the room bridge, in a later release. The shared mailbox runs at `rooms.milim.dev`.

It uses only the Node.js standard library (Node 22.13 or later, for `node:sqlite`) and keeps everything in one SQLite file.

## Self-hosting

```shell
cd mailbox/deploy

# 1. Set PUBLIC_URL; optionally a registration token and limits
cp .env.example .env

# 2. Create the data directory, owned by the image's node user (uid 1000)
mkdir -p data
sudo chown 1000:1000 data

# 3. Build and run
docker compose up -d
```

If you skip step 2, Docker creates `data/` owned by root on a Linux host. The mailbox runs as `node`, can't write there, and exits at startup naming the directory.

The mailbox listens on port 9130. Put it behind an HTTPS reverse proxy, at the root of its own host name, and set `PUBLIC_URL` to that URL. Owners sign its host name and the request path, so the proxy must pass the path on unchanged, with no prefix. The Compose file joins the external `pangolin` network and sets `TRUST_PROXY=1`; change both to match your proxy. With `TRUST_PROXY` set, the mailbox rate-limits by the address the outermost trusted proxy appended to `X-Forwarded-For`. That holds only when nothing but the proxy can reach port 9130: anything else that can picks the address it's counted as, and could get a real client turned away. So don't publish the port, and if other containers share the proxy's network, give the mailbox a network of its own that only the proxy also joins. The proxy must also append that address (Traefik, which Pangolin uses, does by default): a request without it counts against the proxy's own address, which every client then shares.

Without Docker, from this directory:

```shell
PUBLIC_URL=http://127.0.0.1:9130 DATA_PATH=./data/mailbox.sqlite npm start
npm test
```

| Variable | Default | Meaning |
|---|---|---|
| `DATA_PATH` | required | The SQLite file. Its directory must be writable, because SQLite keeps its `-wal` and `-shm` files beside it. |
| `PUBLIC_URL` | required | The URL owners reach the mailbox at, such as `https://rooms.example.com`, with no path. Their signatures are checked against its host name. |
| `HOST`, `PORT` | `127.0.0.1`, `9130` | Where the mailbox listens. |
| `TRUST_PROXY` | `0` | How many reverse proxies are in front of the mailbox (0 to 10). |
| `MAILBOX_REGISTRATION_TOKEN` | empty | Empty means open registration. When it's set (at least 16 characters, no spaces), only a registration that sends it as `Mailbox-Registration` can make an inbox. |
| `MAILBOX_*` | see [Limits](#limits) | Capacity limits. |

Run one mailbox process per `DATA_PATH`. Its rate limits, replay protection and waiting polls live in that process. A restart forgets which signatures it has seen, so an owner request captured in the five minutes before a restart could be sent once more after it. Requests travel over HTTPS, so only someone who can read that traffic could capture one.

## Protocol v1

**Inboxes.** An installation's inbox is named after its Ed25519 public key: the id is the unpadded base64url SHA-256 of the raw 32-byte key.

**Owner requests** are signed with that key:

```
Authorization: Mailbox-Ed25519 ts=<unix seconds>,nonce=<16 to 64 base64url characters>,sig=<base64url Ed25519 signature>
```

The signature covers these UTF-8 bytes, with each `\n` a newline:

```
mailbox-v1\n<METHOD>\n<host>\n<path and query, exactly as sent>\n<ts>\n<nonce>\n<base64url SHA-256 of the body>
```

`<host>` is the host of the URL the owner reaches the mailbox at, the way a URL parser gives it: lowercase, with the port only when it isn't the scheme's default (`rooms.milim.dev`). The mailbox checks it against its own `PUBLIC_URL`, never the request's `Host` header, so a request signed for one mailbox can't pass at another, though an inbox has the same id on both. The timestamp must be within 300 seconds of the mailbox's clock, and each signature is accepted once. Ed25519 signs the same bytes the same way, so use a fresh random nonce for every request. Otherwise a retry, or a second poll in the same second, looks like a replay.

**Send tokens.** An owner gives each linked installation a secret send token, and registers the token's SHA-256 (unpadded base64url) under a sender id it picks (`[A-Za-z0-9_-]{1,64}`). The sender sends `Authorization: Bearer <token>`. The mailbox stores only the hash.

| Route | Who | Does |
|---|---|---|
| `GET /healthz` | anyone | `{"ok": true}` |
| `POST /v1/inboxes` `{"key"}` | owner, signed by `key` | Makes the inbox (201) or confirms it (200), and answers `{"inbox", "limits": {"max_envelope_bytes", "ttl_seconds", "max_wait_seconds"}}`. |
| `GET /v1/inboxes/{inbox}` | owner | `{"inbox", "pending", "pending_bytes", "senders"}` |
| `DELETE /v1/inboxes/{inbox}` | owner | Deletes the inbox with its senders and envelopes (204). |
| `PUT /v1/inboxes/{inbox}/senders/{sender}` `{"token_sha256"}` | owner | Adds a sender or gives it a new token (204). Its pending envelopes stay. |
| `DELETE /v1/inboxes/{inbox}/senders/{sender}` | owner | Removes the sender and drops what it left (204, or 404 if there was none). |
| `GET /v1/inboxes/{inbox}/senders` | owner | `{"senders": [{"sender", "pending", "last_sent_at"}]}` |
| `POST /v1/inboxes/{inbox}/envelopes` (raw bytes) | sender, with an optional `Idempotency-Key` (`[A-Za-z0-9_.:-]{1,128}`) | Stores the envelope and answers `{"expires_at"}` (201). The same key again, while that envelope is pending, answers the same (200) instead of storing a copy. |
| `GET /v1/inboxes/{inbox}/envelopes?after=&limit=&wait=` | owner | Envelopes after `seq` `after`, oldest first: `{"envelopes": [{"seq", "sender", "received_at", "body"}], "next"}`, with each body in standard base64. `limit` is 1 to 100 (default 50), and a page also stops at 1 MB of envelopes (never fewer than one), so its answer stays under about 1.4 MB. With `wait` (seconds, cut to `max_wait_seconds`), an empty inbox holds the request until an envelope arrives or the wait ends. Poll again with `after=next`. |
| `POST /v1/inboxes/{inbox}/ack` `{"through"}`, `{"seqs"}` or both | owner | Deletes the envelopes up to and including `through`, and those listed in `seqs` (up to 100), and answers `{"deleted"}`. |

**Seqs.** Each inbox numbers its own envelopes, so the numbers say nothing about other inboxes' traffic. Senders aren't told the seq: the gap since a sender's last one would say how much the inbox got from other links. An inbox's first seq is its creation time in Unix seconds times 1,000, plus 1, and each envelope after takes the next, so `after=0` reads from the start. An inbox made again under the same key numbers on past the earlier one, unless that one averaged more than a thousand envelopes a second. Registration answers 201 only when it makes the inbox, so start again from `after=0` then.

Every error is `{"error": "<code>"}`. An unknown inbox, a wrong token, and a bad or replayed signature all get 401 `unauthorized`, and an unknown inbox takes as long to refuse as a bad signature, so neither the answer nor its timing says whether an inbox exists. The other codes:

| Status | Codes |
|---|---|
| 400 | `invalid_json`, `invalid_key`, `invalid_token_hash`, `invalid_query`, `invalid_ack`, `invalid_idempotency_key`, `empty_envelope` |
| 403 | `registration_closed` |
| 404 | `not_found` |
| 409 | `token_in_use` (another sender of the inbox has that token), `too_many_senders` |
| 413 | `envelope_too_large`, `payload_too_large` (an owner request over 16 KB) |
| 429 | `rate_limited`, `inbox_full`, `sender_backlog`, `too_many_waiters` |
| 507 | `mailbox_full` |

An envelope that isn't acknowledged within `ttl_seconds` (7 days) is deleted. So is an inbox whose owner hasn't made a request for `MAILBOX_INBOX_IDLE_DAYS` (90 days), along with everything it holds.

## Limits

Each bound is a backstop against floods, sized far above what linked installations use. Every one can be changed with the variable next to it: unset or empty keeps the default, and anything other than a positive integer stops the mailbox at boot.

| Bound | Default | Variable |
|---|---|---|
| Envelope size (the protocol's 256 KB) | 262144 bytes | `MAILBOX_MAX_ENVELOPE_BYTES` |
| How long an envelope waits (the protocol's 7 days) | 604800 s | `MAILBOX_TTL_SECONDS` |
| Longest poll wait, under common proxy timeouts | 25 s | `MAILBOX_MAX_WAIT_SECONDS` |
| Inboxes | 10000 | `MAILBOX_MAX_INBOXES` |
| Senders per inbox | 64 | `MAILBOX_MAX_SENDERS_PER_INBOX` |
| Pending envelopes per inbox | 2000 | `MAILBOX_MAX_PENDING_PER_INBOX` |
| Pending bytes per inbox | 67108864 | `MAILBOX_MAX_PENDING_BYTES_PER_INBOX` |
| Pending envelopes per sender, so one link can't fill an inbox | 500 | `MAILBOX_MAX_PENDING_PER_SENDER` |
| Bytes stored in all | 4294967296 | `MAILBOX_MAX_TOTAL_BYTES` |
| Sends per minute per sender | 600 | `MAILBOX_SENDS_PER_MINUTE_PER_SENDER` |
| Owner requests per minute per inbox | 600 | `MAILBOX_OWNER_REQUESTS_PER_MINUTE_PER_INBOX` |
| New inboxes per minute, mailbox-wide | 24 | `MAILBOX_REGISTRATIONS_PER_MINUTE` |
| New inboxes per hour per client address | 10 | `MAILBOX_REGISTRATIONS_PER_HOUR_PER_CLIENT` |
| Failed authentications per minute per client address, before it's turned away until the minute ends | 120 | `MAILBOX_FAILED_AUTH_PER_MINUTE_PER_CLIENT` |
| Waiting polls, mailbox-wide | 2000 | `MAILBOX_MAX_WAITERS` |
| Waiting polls per inbox | 8 | `MAILBOX_MAX_WAITERS_PER_INBOX` |
| Days before an unread inbox is deleted | 90 | `MAILBOX_INBOX_IDLE_DAYS` |

Registering a new inbox counts against the registration rates; registering one that exists counts as an owner request. Each inbox has its own room for the signatures it has used, twelve minutes of its owner rate (7,200 by default), so an owner that fills it is answered `rate_limited` until its oldest signatures expire, and no other owner notices. A remembered signature takes about 150 bytes: the rooms use that much for each owner request accepted in the last five minutes (up to ten, for an owner whose clock runs ahead), and one room at most about 1 MB. The per-address rates follow up to 100,000 addresses at a time: past that, new addresses go uncounted rather than anyone being turned away, and the mailbox-wide registration rate still holds.

## What the mailbox keeps

- For each inbox: its public key, when it was made, when its owner last made a request, and the last seq it gave an envelope.
- For each sender: the id its owner chose, the SHA-256 of its token, and when it last sent.
- For each pending envelope: its sealed bytes, its size, the sender id, its idempotency key, and when it arrived and expires.

Removing a sender or deleting an inbox drops its pending envelopes at once. The SQLite file and its WAL are created readable by the mailbox's user only.

Each request logs its method, its route pattern (such as `/v1/inboxes/:inbox/envelopes`), its status and how long it took. No log line names an inbox or a sender, or holds a token, a signature or a body.

The mailbox can't read envelopes, but whoever runs it can see traffic: which inboxes get envelopes from which sender ids, how many, how large, and when. If that matters to you, run your own mailbox and link through it.
