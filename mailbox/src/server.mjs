import { createHash, generateKeyPairSync, timingSafeEqual } from 'node:crypto';
import { mkdirSync, realpathSync, unlinkSync, writeFileSync } from 'node:fs';
import { createServer } from 'node:http';
import { isIP } from 'node:net';
import { dirname } from 'node:path';
import { pathToFileURL } from 'node:url';
import { ReplayCache, inboxIdFor, parseKey, tokenHash, verifyOwnerSignature } from './auth.mjs';
import { limitsFromEnv } from './limits.mjs';
import { RateLimits } from './rates.mjs';
import { MailboxError, MailboxStore } from './store.mjs';

const MINUTE_MS = 60_000;
const HOUR_MS = 3_600_000;
const PRUNE_INTERVAL_MS = 60_000;
// Owner requests carry small JSON bodies: a key, a token hash, an ack.
const OWNER_BODY_MAX_BYTES = 16_384;
const PAGE_DEFAULT_ENVELOPES = 50;
const PAGE_MAX_ENVELOPES = 100;
const ACK_MAX_SEQS = 100;
const INBOX_ROUTE = /^\/v1\/inboxes\/([A-Za-z0-9_-]{43})(?:\/(senders|envelopes|ack)(?:\/([A-Za-z0-9_-]{1,64}))?)?$/;
const TOKEN_HASH_PATTERN = /^[A-Za-z0-9_-]{43}$/;
const BEARER_PATTERN = /^Bearer ([\x21-\x7e]{1,512})$/;
const IDEMPOTENCY_KEY_PATTERN = /^[A-Za-z0-9_.:-]{1,128}$/;
const WHOLE_NUMBER_PATTERN = /^\d{1,15}$/;
// A request to an unknown inbox is checked against this key, whose private
// half is thrown away, so refusing it takes as long as refusing a bad
// signature and the timing doesn't say which inboxes exist.
const STAND_IN_KEY = Buffer.from(generateKeyPairSync('ed25519').publicKey.export({ format: 'jwk' }).x, 'base64url');

const jsonLog = {
  info: (fields) => console.log(JSON.stringify({ level: 'info', ...fields })),
  error: (fields) => console.error(JSON.stringify({ level: 'error', ...fields })),
};

// The mailbox's HTTP server over `store`. Nothing it logs names an inbox,
// a sender, a token or a signature: only routes, statuses and timings.
export function createMailbox({
  store, limits, registrationToken, proxyHops = 0,
  // Room for twelve minutes of owner requests per inbox: a signature is
  // remembered for ten at most.
  replay = new ReplayCache(12 * limits.ownerRequestsPerMinutePerInbox),
  clientBuckets = 100_000, log = jsonLog,
}) {
  // Per inbox, per sender and mailbox-wide, counted only for requests that
  // authenticated, so the store's own limits bound the keys.
  const rates = new RateLimits();
  // Per client address, which a flood can rotate: past `clientBuckets`
  // addresses, new ones go uncounted rather than locking anyone out.
  const clientRates = new RateLimits(clientBuckets);
  const polls = new Polls(limits);
  const registrationDigest = registrationToken === undefined ? undefined : digest(registrationToken);

  const server = createServer({ requestTimeout: 60_000, headersTimeout: 20_000 }, async (request, response) => {
    const startedAt = Date.now();
    const context = { route: 'unknown' };
    try {
      await route(request, response, context);
    } catch (error) {
      if (response.destroyed) return;
      const known = error instanceof MailboxError;
      if (!known) log.error({ message: 'request failed', route: context.route, error: error instanceof Error ? error.message : String(error) });
      sendError(response, known ? error : new MailboxError('internal_error', 500));
    } finally {
      if (context.route !== '/healthz') {
        // 499: the client left before an answer (a poll it gave up on).
        log.info({ method: request.method, route: context.route, status: response.headersSent ? response.statusCode : 499, duration_ms: Date.now() - startedAt });
      }
    }
  });

  async function route(request, response, context) {
    const target = request.url ?? '';
    const queryAt = target.indexOf('?');
    const path = queryAt === -1 ? target : target.slice(0, queryAt);
    const client = clientAddress(request, proxyHops);
    if (path === '/healthz' && request.method === 'GET') {
      context.route = '/healthz';
      return sendJson(response, 200, { ok: true });
    }
    if (path === '/v1/inboxes' && request.method === 'POST') {
      context.route = '/v1/inboxes';
      return register(request, response, client);
    }
    const match = INBOX_ROUTE.exec(path);
    if (!match || (match[3] && match[2] !== 'senders')) throw new MailboxError('not_found', 404);
    const [, inbox, collection, sender] = match;
    context.route = `/v1/inboxes/:inbox${collection ? `/${collection}` : ''}${sender ? '/:sender' : ''}`;
    switch (`${request.method} ${context.route}`) {
      case 'POST /v1/inboxes/:inbox/envelopes':
        return deliver(request, response, client, inbox);
      case 'GET /v1/inboxes/:inbox/envelopes':
        await owner(request, client, inbox);
        return poll(response, inbox, new URLSearchParams(queryAt === -1 ? '' : target.slice(queryAt + 1)));
      case 'POST /v1/inboxes/:inbox/ack': {
        const { through, seqs } = parseJson(await owner(request, client, inbox));
        const validThrough = through === undefined || (Number.isSafeInteger(through) && through >= 0);
        const validSeqs = seqs === undefined || (Array.isArray(seqs) && seqs.length >= 1 && seqs.length <= ACK_MAX_SEQS
          && seqs.every((seq) => Number.isSafeInteger(seq) && seq > 0));
        if ((through === undefined && seqs === undefined) || !validThrough || !validSeqs) throw new MailboxError('invalid_ack', 400);
        return sendJson(response, 200, { deleted: store.ack(inbox, { through, seqs }) });
      }
      case 'GET /v1/inboxes/:inbox':
        await owner(request, client, inbox);
        return sendJson(response, 200, { inbox, ...store.inboxStatus(inbox) });
      case 'DELETE /v1/inboxes/:inbox':
        await owner(request, client, inbox);
        store.deleteInbox(inbox);
        polls.wake(inbox, 'timeout');
        return sendEmpty(response, 204);
      case 'GET /v1/inboxes/:inbox/senders':
        await owner(request, client, inbox);
        return sendJson(response, 200, { senders: store.listSenders(inbox) });
      case 'PUT /v1/inboxes/:inbox/senders/:sender': {
        const { token_sha256: hash } = parseJson(await owner(request, client, inbox));
        if (typeof hash !== 'string' || !TOKEN_HASH_PATTERN.test(hash)) throw new MailboxError('invalid_token_hash', 400);
        store.putSender(inbox, sender, hash);
        return sendEmpty(response, 204);
      }
      case 'DELETE /v1/inboxes/:inbox/senders/:sender':
        await owner(request, client, inbox);
        if (!store.deleteSender(inbox, sender)) throw new MailboxError('not_found', 404);
        return sendEmpty(response, 204);
      default:
        throw new MailboxError('not_found', 404);
    }
  }

  async function register(request, response, client) {
    turnAwayFailing(client);
    const body = await readBody(request, OWNER_BODY_MAX_BYTES, 'payload_too_large');
    const key = parseKey(parseJson(body).key);
    if (!key) throw new MailboxError('invalid_key', 400);
    if (registrationDigest && !sameSecret(request.headers['mailbox-registration'], registrationDigest)) {
      refused(client);
      throw new MailboxError('registration_closed', 403);
    }
    const inbox = inboxIdFor(key);
    const now = Math.floor(Date.now() / 1000);
    const signed = ownerSignature(request, key, body, now);
    if (!signed || replay.seen(inbox, signed.signature, now)) throw refused(client);
    if (store.inboxKey(inbox)) {
      // Confirming an inbox is an owner request like any other.
      admitOwner(inbox, signed, now);
    } else if (!clientRates.take(`register:${client}`, limits.registrationsPerHourPerClient, HOUR_MS)
      || !rates.take('register', limits.registrationsPerMinute, MINUTE_MS)
      // A new key's signature is claimed only once the registration rates
      // let it through, so fresh keys can't take room faster than inboxes
      // can be made.
      || !replay.claim(inbox, signed.signature, now, signed.until)) {
      throw new MailboxError('rate_limited', 429);
    }
    const { created } = store.register(key);
    sendJson(response, created ? 201 : 200, {
      inbox,
      limits: { max_envelope_bytes: limits.maxEnvelopeBytes, ttl_seconds: limits.ttlSeconds, max_wait_seconds: limits.maxWaitSeconds },
    });
  }

  // A linked sender leaves an envelope with its send token. The body is
  // read only once the token checks out.
  async function deliver(request, response, client, inbox) {
    turnAwayFailing(client);
    const bearer = BEARER_PATTERN.exec(request.headers.authorization ?? '');
    const sender = bearer ? store.senderFor(inbox, tokenHash(bearer[1])) : undefined;
    if (!sender) throw refused(client);
    if (!rates.take(`send:${inbox}:${sender}`, limits.sendsPerMinutePerSender, MINUTE_MS)) throw new MailboxError('rate_limited', 429);
    const idempotencyKey = request.headers['idempotency-key'];
    if (idempotencyKey !== undefined && !IDEMPOTENCY_KEY_PATTERN.test(idempotencyKey)) throw new MailboxError('invalid_idempotency_key', 400);
    const body = await readBody(request, limits.maxEnvelopeBytes, 'envelope_too_large');
    // The owner may have removed the sender, or changed its token, while the body arrived.
    if (store.senderFor(inbox, tokenHash(bearer[1])) !== sender) throw new MailboxError('unauthorized', 401);
    const receipt = store.put(inbox, sender, body, idempotencyKey ?? null);
    if (!receipt.duplicate) polls.wake(inbox);
    sendJson(response, receipt.duplicate ? 200 : 201, { seq: receipt.seq, expires_at: receipt.expires_at });
  }

  // Envelopes after `after`, oldest first. With `wait`, an empty inbox holds
  // the poll until a send wakes it or the wait runs out.
  async function poll(response, inbox, query) {
    const after = wholeNumber(query, 'after', 0);
    const limit = Math.min(wholeNumber(query, 'limit', PAGE_DEFAULT_ENVELOPES), PAGE_MAX_ENVELOPES);
    const wait = Math.min(wholeNumber(query, 'wait', 0), limits.maxWaitSeconds);
    if (limit < 1) throw new MailboxError('invalid_query', 400);
    let page = store.list(inbox, { after, limit });
    if (!page.length && wait > 0) {
      polls.admit(inbox);
      const deadline = Date.now() + wait * 1000;
      for (;;) {
        const outcome = await polls.next(inbox, deadline - Date.now(), response);
        if (outcome === 'gone') return;
        page = store.list(inbox, { after, limit });
        if (page.length || outcome === 'timeout') break;
      }
    }
    sendJson(response, 200, {
      envelopes: page.map(({ seq, sender, received_at: receivedAt, body }) => ({ seq, sender, received_at: receivedAt, body: body.toString('base64') })),
      next: page.length ? page.at(-1).seq : after,
    });
  }

  // The request's body, when the inbox's owner signed it. An unknown inbox,
  // a bad signature and a replay all get the same 401.
  async function owner(request, client, inbox) {
    turnAwayFailing(client);
    const body = await readBody(request, OWNER_BODY_MAX_BYTES, 'payload_too_large');
    const key = store.inboxKey(inbox);
    const now = Math.floor(Date.now() / 1000);
    const signed = ownerSignature(request, key ?? STAND_IN_KEY, body, now);
    if (!key || !signed || replay.seen(inbox, signed.signature, now)) throw refused(client);
    admitOwner(inbox, signed, now);
    store.touch(inbox);
    return body;
  }

  function ownerSignature(request, key, body, now) {
    return verifyOwnerSignature({ key, method: request.method, target: request.url, body, authorization: request.headers.authorization, now });
  }

  // Claims an owner's checked signature, then counts the request against its
  // inbox's rate. The claim comes first, so a request refused for its rate
  // can't be sent again once the rate resets; an inbox out of room for
  // claims is slowed down alone.
  function admitOwner(inbox, signed, now) {
    if (!replay.claim(inbox, signed.signature, now, signed.until)
      || !rates.take(`owner:${inbox}`, limits.ownerRequestsPerMinutePerInbox, MINUTE_MS)) {
      throw new MailboxError('rate_limited', 429);
    }
  }

  // Counts a failed authentication against the client; the 401 to throw.
  function refused(client) {
    clientRates.take(`failed:${client}`, limits.failedAuthPerMinutePerClient, MINUTE_MS);
    return new MailboxError('unauthorized', 401);
  }

  function turnAwayFailing(client) {
    if (clientRates.exceeded(`failed:${client}`, limits.failedAuthPerMinutePerClient)) throw new MailboxError('rate_limited', 429);
  }

  const pruner = setInterval(() => {
    try {
      replay.sweep(Math.floor(Date.now() / 1000));
      const pruned = store.prune();
      if (pruned.envelopes || pruned.inboxes) log.info({ message: 'pruned', ...pruned });
    } catch (error) {
      log.error({ message: 'prune failed', error: error instanceof Error ? error.message : String(error) });
    }
  }, PRUNE_INTERVAL_MS);
  pruner.unref();

  let closing;
  function close() {
    closing ??= new Promise((resolve) => {
      clearInterval(pruner);
      // Answered now, each owner polls again once the mailbox is back.
      polls.endAll();
      server.close(() => resolve());
      // Keep-alive sockets, the answered polls' among them, would hold close() open.
      setImmediate(() => server.closeAllConnections());
    });
    return closing;
  }

  return { server, close };
}

// Owner polls waiting for an inbox's next envelope. A send wakes every poll
// of its inbox; a client that goes away frees its place at once.
class Polls {
  constructor(limits) {
    this.limits = limits;
    this.byInbox = new Map();
    this.count = 0;
  }

  // Refuses a poll past the mailbox's or the inbox's share of waiting places.
  admit(inbox) {
    if (this.count >= this.limits.maxWaiters || (this.byInbox.get(inbox)?.size ?? 0) >= this.limits.maxWaitersPerInbox) {
      throw new MailboxError('too_many_waiters', 429);
    }
  }

  // 'arrived' on the inbox's next envelope, 'timeout' after `ms`, or 'gone'
  // once the client has closed the connection.
  next(inbox, ms, response) {
    return new Promise((resolve) => {
      if (response.destroyed) {
        resolve('gone');
        return;
      }
      let waiting = this.byInbox.get(inbox);
      if (!waiting) this.byInbox.set(inbox, (waiting = new Set()));
      const finish = (outcome) => {
        if (!waiting.delete(finish)) return;
        if (!waiting.size && this.byInbox.get(inbox) === waiting) this.byInbox.delete(inbox);
        this.count -= 1;
        clearTimeout(timer);
        response.off('close', gone);
        resolve(outcome);
      };
      const gone = () => finish('gone');
      const timer = setTimeout(finish, Math.max(ms, 0), 'timeout');
      waiting.add(finish);
      this.count += 1;
      response.on('close', gone);
    });
  }

  wake(inbox, outcome = 'arrived') {
    for (const finish of [...(this.byInbox.get(inbox) ?? [])]) finish(outcome);
  }

  endAll() {
    for (const inbox of [...this.byInbox.keys()]) this.wake(inbox, 'timeout');
  }
}

// The request's body, at most `maxBytes`: past that, `code` (413). A body
// that says it is too large is refused before any of it is read.
function readBody(request, maxBytes, code) {
  return new Promise((resolve, reject) => {
    if (Number(request.headers['content-length']) > maxBytes) {
      reject(new MailboxError(code, 413));
      return;
    }
    const chunks = [];
    let size = 0;
    request.on('data', (chunk) => {
      size += chunk.length;
      if (size > maxBytes) reject(new MailboxError(code, 413));
      else chunks.push(chunk);
    });
    request.on('end', () => resolve(Buffer.concat(chunks)));
    request.on('error', reject);
    request.on('close', () => reject(new Error('client went away')));
  });
}

// Owner bodies are JSON objects; anything else is a client mistake.
function parseJson(body) {
  let value;
  try {
    value = JSON.parse(body.toString('utf8'));
  } catch {
    throw new MailboxError('invalid_json', 400);
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new MailboxError('invalid_json', 400);
  return value;
}

function wholeNumber(query, name, fallback) {
  const raw = query.get(name);
  if (raw === null) return fallback;
  if (!WHOLE_NUMBER_PATTERN.test(raw)) throw new MailboxError('invalid_query', 400);
  return Number(raw);
}

function digest(secret) {
  return createHash('sha256').update(secret, 'utf8').digest();
}

function sameSecret(provided, expected) {
  return typeof provided === 'string' && timingSafeEqual(digest(provided), expected);
}

function sendJson(response, status, body, headers = {}) {
  if (response.headersSent || response.destroyed) return;
  const encoded = JSON.stringify(body);
  response.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': Buffer.byteLength(encoded),
    'cache-control': 'no-store',
    'x-content-type-options': 'nosniff',
    ...headers,
  });
  response.end(encoded);
}

function sendEmpty(response, status) {
  if (response.headersSent || response.destroyed) return;
  response.writeHead(status, { 'cache-control': 'no-store' });
  response.end();
}

function sendError(response, error) {
  // A refused body may still be arriving: closing the connection stops it.
  sendJson(response, error.status, { error: error.code }, error.status === 413 ? { connection: 'close' } : {});
}

function clientAddress(request, proxyHops) {
  // Only the entries the trusted proxies appended count: earlier ones are
  // whatever the client sent, so taking the first would let anyone pick
  // their own rate-limit bucket.
  if (proxyHops > 0) {
    const forwarded = String(request.headers['x-forwarded-for'] ?? '').split(',').map((entry) => entry.trim());
    const address = forwarded.length >= proxyHops ? forwarded[forwarded.length - proxyHops] : '';
    if (isIP(address)) return address;
  }
  return request.socket.remoteAddress ?? 'unknown';
}

export function readConfig(env = process.env) {
  if (!env.DATA_PATH) throw new Error('DATA_PATH is required.');
  const proxyHops = env.TRUST_PROXY && /^\d{1,2}$/.test(env.TRUST_PROXY) ? Number(env.TRUST_PROXY) : env.TRUST_PROXY ? NaN : 0;
  if (!Number.isInteger(proxyHops) || proxyHops > 10) {
    throw new Error('TRUST_PROXY must be the number of reverse proxies in front of the mailbox (0 to 10).');
  }
  const port = env.PORT ? (/^\d{1,5}$/.test(env.PORT) ? Number(env.PORT) : NaN) : 9130;
  if (!Number.isInteger(port) || port < 1 || port > 65_535) throw new Error('PORT must be a TCP port (1 to 65535).');
  // Compose passes an unset variable as an empty string: open registration.
  const registrationToken = env.MAILBOX_REGISTRATION_TOKEN || undefined;
  if (registrationToken !== undefined && !/^[\x21-\x7e]{16,256}$/.test(registrationToken)) {
    throw new Error('MAILBOX_REGISTRATION_TOKEN must be at least 16 characters, with no spaces.');
  }
  return { host: env.HOST || '127.0.0.1', port, dataPath: env.DATA_PATH, proxyHops, registrationToken, limits: limitsFromEnv(env) };
}

// SQLite opens a missing file by creating it, so a data directory the
// mailbox can't write fails here with what to do, not with SQLite's error.
// A Compose bind mount Docker created itself is root's; the image runs as node.
function assertDataDirectoryWritable(dataPath) {
  const directory = dirname(dataPath);
  const probe = `${dataPath}.${process.pid}.probe`;
  try {
    mkdirSync(directory, { recursive: true, mode: 0o700 });
    writeFileSync(probe, '', { flag: 'wx', mode: 0o600 });
    unlinkSync(probe);
  } catch (error) {
    const uid = process.getuid?.();
    throw new Error(`DATA_PATH directory ${directory} is not writable${uid === undefined ? '' : ` by uid ${uid}`} (${error?.code ?? error}). `
      + 'With the Compose file, create it owned by the image\'s node user first: mkdir -p data && sudo chown 1000:1000 data');
  }
}

function main() {
  // The database and its WAL hold every pending envelope: owner-only.
  process.umask(0o077);
  const config = readConfig(process.env);
  assertDataDirectoryWritable(config.dataPath);
  const store = new MailboxStore(config.dataPath, config.limits);
  store.prune();
  const mailbox = createMailbox({ store, limits: config.limits, registrationToken: config.registrationToken, proxyHops: config.proxyHops });
  // A port in use or a host it can't bind, as a log line rather than a stack.
  mailbox.server.on('error', (error) => {
    jsonLog.error({ message: 'server error', error: error.message });
    process.exit(1);
  });
  mailbox.server.listen(config.port, config.host, () => {
    jsonLog.info({ message: 'Rooms mailbox listening', host: config.host, port: config.port, registration: config.registrationToken ? 'token' : 'open' });
  });
  const shutdown = async () => {
    try {
      await mailbox.close();
      store.close();
    } catch (error) {
      jsonLog.error({ message: 'shutdown failed', error: error instanceof Error ? error.message : String(error) });
      process.exit(1);
    }
    process.exit(0);
  };
  process.once('SIGTERM', shutdown);
  process.once('SIGINT', shutdown);
}

// Start only when run directly, so the tests can import createMailbox.
// argv[1] goes through realpath so a symlinked entrypoint still matches.
function isEntryPoint() {
  if (!process.argv[1]) return false;
  try {
    return pathToFileURL(realpathSync(process.argv[1])).href === import.meta.url;
  } catch {
    return false;
  }
}

if (isEntryPoint()) main();
