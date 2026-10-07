import { createHash, randomBytes, timingSafeEqual } from 'node:crypto';

// Wrist-down tools for a Conduit Watch call (hermes-conduit
// designs/apple-watch-voice-direct.md, "Wrist-down tools through the relay").
//
// With the wrist down the Watch can't reach Conduit on the iPhone, but it can
// reach this relay over its own internet. For each call the Hermes plugin
// opens a short-lived grant here; the Watch posts a sealed tool call to it and
// waits, the plugin picks the call up by long-polling, runs it, and posts the
// sealed answer back, which this relay hands to the waiting Watch request.
//
// The relay is a rendezvous only. Calls and answers are sealed on the Watch
// and the host with a per-call key this relay never sees, and each is bound to
// its grant, call id and direction, so the relay can neither read nor forge
// one. The Watch authenticates with a per-grant relay key, of which the relay
// keeps only the SHA-256. Nothing here is persisted: a restart drops every
// grant, and both ends fall back to the iPhone path.

export const WATCH_TOOLS_CAPABILITY = 'watch-tools-v1';

// How long a Watch call waits for the host's answer. The host's own lookups
// give up first (web search at 20 s, memory at 12 s), so a slow backend ends
// in the host's answer rather than here.
export const CALL_WAIT_MS = 25_000;
// The longest a host poll is held open when no call is waiting.
export const HOST_POLL_WAIT_MS = 25_000;
// A grant whose host hasn't polled for this long has lost its host (a Hermes
// restart drops the plugin's grants): its calls fail at once instead of
// waiting out CALL_WAIT_MS.
export const HOST_GONE_MS = 40_000;
export const MIN_TTL_S = 60;
export const MAX_TTL_S = 1800;
export const MAX_CALLS_PER_GRANT = 120;
// Calls of one grant waiting for an answer at once.
export const MAX_PENDING_PER_GRANT = 2;
// Sealed sizes, base64url: a call is a tool name and a short query; an
// answer is at most a few search results or a 4,000-character recall. A call
// is at most 4 KB before sealing on the Watch and the host alike, so the
// relay takes no call the host would refuse to open.
export const MAX_CALL_BYTES = 4 * 1024;
export const MAX_CALL_CT_CHARS = Math.ceil(((MAX_CALL_BYTES + 16) * 4) / 3);
export const MAX_RESULT_CT_CHARS = 24_000;

const ID_PATTERN = /^[A-Za-z0-9_-]{22}$/;
const NONCE_PATTERN = /^[A-Za-z0-9_-]{16}$/;
const WATCH_KEY_PATTERN = /^[A-Za-z0-9_-]{43}$/;
const KEY_HASH_PATTERN = /^[0-9a-f]{64}$/;
const CT_PATTERN = /^[A-Za-z0-9_-]+$/;
const ROUTE = /^\/v1\/watch-tools\/grants(?:\/([A-Za-z0-9_-]{1,64})(?:\/(calls|results)(?:\/([A-Za-z0-9_-]{1,64}))?)?)?$/;

export class WatchToolGrants {
  constructor({
    maxGrants,
    maxPerGateway,
    callWaitMs = CALL_WAIT_MS,
    hostPollWaitMs = HOST_POLL_WAIT_MS,
    hostGoneMs = HOST_GONE_MS,
    now = Date.now,
  }) {
    this.maxGrants = maxGrants;
    this.maxPerGateway = maxPerGateway;
    this.callWaitMs = callWaitMs;
    this.hostPollWaitMs = hostPollWaitMs;
    this.hostGoneMs = hostGoneMs;
    this.now = now;
    this.grants = new Map();
  }

  get size() {
    return this.grants.size;
  }

  create({ installationId, gatewayId, watchKeyHash, ttlS, maxCalls }) {
    this.sweep();
    const owned = [...this.grants.values()]
      .filter((grant) => grant.installationId === installationId && grant.gatewayId === gatewayId)
      .sort((a, b) => a.createdAt - b.createdAt);
    // A host that lost track of its grants (a restart) must still be able to
    // open new ones, so the oldest makes way instead of the new one failing.
    while (owned.length >= this.maxPerGateway) this.close(owned.shift(), 'replaced');
    if (this.grants.size >= this.maxGrants) throw httpError(503, 'watch_grant_capacity');
    const now = this.now();
    let id;
    do id = randomBytes(16).toString('base64url'); while (this.grants.has(id));
    const grant = {
      id,
      installationId,
      gatewayId,
      keyHash: Buffer.from(watchKeyHash, 'hex'),
      createdAt: now,
      expiresAt: now + ttlS * 1000,
      maxCalls,
      calls: 0,
      seen: new Set(),
      // Calls the host hasn't picked up yet: { rid, n, ct }.
      queue: [],
      // Watch requests waiting for their answer, by call id.
      waiting: new Map(),
      hostWaiter: null,
      // A new grant counts as polled: the host starts polling right after
      // creating it.
      hostSeenAt: now,
    };
    this.grants.set(id, grant);
    return grant;
  }

  live(id) {
    const grant = this.grants.get(id);
    if (!grant) return undefined;
    if (grant.expiresAt <= this.now()) {
      this.close(grant, 'expired');
      return undefined;
    }
    return grant;
  }

  owned(id, installationId, gatewayId) {
    const grant = this.live(id);
    return grant && grant.installationId === installationId && grant.gatewayId === gatewayId ? grant : undefined;
  }

  // The Watch's bearer is the grant's relay key; only its SHA-256 is kept.
  authorizedWatch(id, bearer) {
    if (!WATCH_KEY_PATTERN.test(bearer ?? '')) return undefined;
    const grant = this.live(id);
    if (!grant) return undefined;
    const presented = createHash('sha256').update(bearer, 'utf8').digest();
    return timingSafeEqual(presented, grant.keyHash) ? grant : undefined;
  }

  close(grant, reason) {
    if (!this.grants.delete(grant.id)) return;
    for (const entry of grant.waiting.values()) {
      clearTimeout(entry.timer);
      sendJson(entry.response, 410, { error: 'grant_closed', reason });
    }
    grant.waiting.clear();
    grant.queue = [];
    if (grant.hostWaiter) {
      clearTimeout(grant.hostWaiter.timer);
      sendJson(grant.hostWaiter.response, 410, { error: 'grant_closed', reason });
      grant.hostWaiter = null;
    }
  }

  sweep() {
    const now = this.now();
    for (const grant of [...this.grants.values()]) {
      if (grant.expiresAt <= now) this.close(grant, 'expired');
    }
  }

  // The host's long poll: answers at once with any waiting calls, otherwise
  // when one arrives or after `waitMs`.
  poll(grant, response, waitMs) {
    grant.hostSeenAt = this.now();
    if (grant.hostWaiter) {
      // One poll at a time; a newer one takes over.
      const previous = grant.hostWaiter;
      grant.hostWaiter = null;
      clearTimeout(previous.timer);
      sendJson(previous.response, 200, { calls: [] });
    }
    if (grant.queue.length) {
      sendJson(response, 200, { calls: grant.queue.splice(0) });
      return;
    }
    const waiter = { response, timer: null };
    waiter.timer = setTimeout(() => {
      if (grant.hostWaiter !== waiter) return;
      grant.hostWaiter = null;
      // A poll that ran its course: the host is there.
      grant.hostSeenAt = this.now();
      sendJson(response, 200, { calls: [] });
    }, Math.min(waitMs, this.hostPollWaitMs));
    // A poll that dropped says nothing about the host (it may have died), so
    // only a completed or a new poll counts as seeing it.
    response.on('close', () => {
      clearTimeout(waiter.timer);
      if (grant.hostWaiter === waiter) grant.hostWaiter = null;
    });
    grant.hostWaiter = waiter;
  }

  // A Watch call: queued for the host, answered when the host posts the
  // result, or 504 after callWaitMs.
  call(grant, response, { rid, n, ct }) {
    if (grant.seen.has(rid)) return sendJson(response, 409, { error: 'duplicate_call' });
    if (grant.calls >= grant.maxCalls) return sendJson(response, 429, { error: 'grant_exhausted' });
    if (grant.waiting.size >= MAX_PENDING_PER_GRANT) return sendJson(response, 429, { error: 'too_many_calls' });
    if (!grant.hostWaiter && this.now() - grant.hostSeenAt > this.hostGoneMs) {
      return sendJson(response, 503, { error: 'host_offline' });
    }
    grant.seen.add(rid);
    grant.calls += 1;
    const entry = { response, timer: null };
    const drop = () => {
      if (grant.waiting.get(rid) !== entry) return;
      grant.waiting.delete(rid);
      grant.queue = grant.queue.filter((queued) => queued.rid !== rid);
    };
    entry.timer = setTimeout(() => {
      drop();
      sendJson(response, 504, { error: 'host_timeout' });
    }, this.callWaitMs);
    // The Watch gave up (its own timeout, or the call ended): nobody reads
    // the answer, so the host isn't asked for it.
    response.on('close', () => {
      clearTimeout(entry.timer);
      drop();
    });
    grant.waiting.set(rid, entry);
    grant.queue.push({ rid, n, ct });
    if (grant.hostWaiter) {
      const waiter = grant.hostWaiter;
      grant.hostWaiter = null;
      clearTimeout(waiter.timer);
      grant.hostSeenAt = this.now();
      sendJson(waiter.response, 200, { calls: grant.queue.splice(0) });
    }
  }

  // The host's sealed answer to one call.
  result(grant, rid, { n, ct }) {
    const entry = grant.waiting.get(rid);
    if (!entry) return false;
    grant.waiting.delete(rid);
    clearTimeout(entry.timer);
    sendJson(entry.response, 200, { n, ct });
    return true;
  }
}

// Routes under /v1/watch-tools/. Returns false for any other path.
export function watchToolRoutes({
  grants,
  readJson,
  enforceRateLimit,
  enforceCallBudget,
  authenticateGateway,
  clientAddress,
  warn = () => {},
}) {
  return async function route(request, response, url) {
    const match = url.pathname.match(ROUTE);
    if (!match) return false;
    const [, id, kind, rid] = match;
    const client = clientAddress(request);

    if (!id && !kind && request.method === 'POST') {
      // The host opens a grant for one Watch call.
      const gateway = authenticateGateway(request);
      if (!gateway) return answered(response, 401, { error: 'unauthorized' });
      enforceRateLimit(`watch-grant:${gateway.gatewayId}`, 12, 60_000);
      const body = await readJson(request);
      const ttlS = body.ttl_s;
      const maxCalls = body.max_calls;
      if (typeof body.watch_key_sha256 !== 'string' || !KEY_HASH_PATTERN.test(body.watch_key_sha256)
        || !Number.isSafeInteger(ttlS) || ttlS < MIN_TTL_S || ttlS > MAX_TTL_S
        || !Number.isSafeInteger(maxCalls) || maxCalls < 1 || maxCalls > MAX_CALLS_PER_GRANT) {
        return answered(response, 400, { error: 'invalid_grant' });
      }
      let grant;
      try {
        grant = grants.create({
          installationId: gateway.installationId,
          gatewayId: gateway.gatewayId,
          watchKeyHash: body.watch_key_sha256,
          ttlS,
          maxCalls,
        });
      } catch (error) {
        if (error?.message !== 'watch_grant_capacity') throw error;
        // An expected limit, not a relay failure: the call's lookups go
        // through the iPhone instead.
        warn('watch tool grants at capacity', { grants: grants.size });
        return answered(response, 503, { error: 'watch_grant_capacity' });
      }
      return answered(response, 201, {
        grant_id: grant.id,
        expires_at: new Date(grant.expiresAt).toISOString(),
        call_wait_ms: grants.callWaitMs,
      });
    }

    if (!id || !ID_PATTERN.test(id)) return answered(response, 404, { error: 'not_found' });

    if (kind === 'calls' && !rid && request.method === 'POST') {
      // A Watch call. Authenticated and rate-limited before the body is read:
      // this route is public.
      enforceRateLimit(`watch-call-ip:${client}`, 120, 60_000);
      const grant = grants.authorizedWatch(id, bearerToken(request));
      if (!grant) return answered(response, 401, { error: 'unauthorized' });
      enforceRateLimit(`watch-call:${grant.id}`, 30, 60_000);
      enforceCallBudget();
      const body = await readJson(request);
      const sealed = validSealed(body, MAX_CALL_CT_CHARS);
      if (!sealed || typeof body.rid !== 'string' || !ID_PATTERN.test(body.rid)) {
        return answered(response, 400, { error: 'invalid_call' });
      }
      // The grant may have closed while the body was read.
      if (!grants.live(id)) return answered(response, 410, { error: 'grant_closed' });
      grants.call(grant, response, { rid: body.rid, ...sealed });
      return true;
    }

    if (kind === 'calls' && !rid && request.method === 'GET') {
      const gateway = authenticateGateway(request);
      if (!gateway) return answered(response, 401, { error: 'unauthorized' });
      const grant = grants.owned(id, gateway.installationId, gateway.gatewayId);
      if (!grant) return answered(response, 410, { error: 'grant_closed' });
      enforceRateLimit(`watch-poll:${grant.id}`, 240, 60_000);
      // A missing or empty wait_ms is the default, not a 0 ms poll.
      const raw = url.searchParams.get('wait_ms');
      const requested = raw === null || raw.trim() === '' ? NaN : Number(raw);
      grants.poll(grant, response, Number.isFinite(requested) && requested >= 0 ? requested : grants.hostPollWaitMs);
      return true;
    }

    if (kind === 'results' && rid && request.method === 'POST') {
      const gateway = authenticateGateway(request);
      if (!gateway) return answered(response, 401, { error: 'unauthorized' });
      const grant = grants.owned(id, gateway.installationId, gateway.gatewayId);
      if (!grant) return answered(response, 410, { error: 'grant_closed' });
      enforceRateLimit(`watch-result:${grant.id}`, 120, 60_000);
      const body = await readJson(request);
      const sealed = validSealed(body, MAX_RESULT_CT_CHARS);
      if (!sealed || !ID_PATTERN.test(rid)) return answered(response, 400, { error: 'invalid_result' });
      if (!grants.result(grant, rid, sealed)) return answered(response, 404, { error: 'unknown_call' });
      return answered(response, 200, { status: 'delivered' });
    }

    if (!kind && request.method === 'DELETE') {
      // Either end can close the grant: the host when the call ends or the
      // grant is replaced, the Watch when its call ends. Every DELETE gets the
      // same 204, so a wrong key or another gateway's grant reads as gone and
      // doesn't tell whether a grant id is live.
      const gateway = authenticateGateway(request);
      let grant;
      if (gateway) {
        grant = grants.owned(id, gateway.installationId, gateway.gatewayId);
      } else {
        enforceRateLimit(`watch-call-ip:${client}`, 120, 60_000);
        grant = grants.authorizedWatch(id, bearerToken(request));
      }
      if (grant) grants.close(grant, gateway ? 'host' : 'watch');
      response.writeHead(204).end();
      return true;
    }

    return answered(response, 404, { error: 'not_found' });
  };
}

function answered(response, status, body) {
  sendJson(response, status, body);
  return true;
}

function bearerToken(request) {
  const match = String(request.headers.authorization ?? '').match(/^Bearer\s+(\S+)$/i);
  return match ? match[1] : undefined;
}

function validSealed(body, maxCtChars) {
  const { n, ct } = body ?? {};
  if (typeof n !== 'string' || !NONCE_PATTERN.test(n)) return undefined;
  // At least the 16-byte tag.
  if (typeof ct !== 'string' || ct.length < 22 || ct.length > maxCtChars || !CT_PATTERN.test(ct)) return undefined;
  return { n, ct };
}

function sendJson(response, status, body) {
  if (response.headersSent || response.writableEnded || response.destroyed) return;
  const encoded = JSON.stringify(body);
  response.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'content-length': Buffer.byteLength(encoded), 'cache-control': 'no-store', 'x-content-type-options': 'nosniff' });
  response.end(encoded);
}

function httpError(status, code) {
  const error = new Error(code);
  error.status = status;
  return error;
}
