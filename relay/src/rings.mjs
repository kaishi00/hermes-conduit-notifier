import { createHash, randomBytes, timingSafeEqual } from 'node:crypto';

// Hermes calls ringing on the Apple Watch too (hermes-conduit
// designs/hermes-calls-watch.md).
//
// iOS doesn't pass a calling app's CallKit call to the Watch, so a phone that
// registered a Watch PushKit token gets each call on both devices. Both
// pushes carry the same ring: an id and a token that only those pushes hold.
// The device that answers or declines settles the ring here with its token,
// and the relay tells the other device to stop ringing (the iPhone's ringing
// call stops its link to the Watch, so the devices can't tell each other).
// The first settle wins. Nothing is persisted: a restart drops every ring,
// and the other device rings out as it would have.
//
// A call answered on the Watch starts through here too ("Starting an
// answered call through the relay"): the Watch's link to the iPhone stays
// down under the system call screen, so its start rides in its settle
// (`start`) and on to the phone in the stop push, and the phone stores the
// call's session here for the Watch to fetch with the ring's token. Both are
// sealed with a key only the two devices hold: the relay sees their size.

// Long enough for a push that reached the device late (the phone rings one up
// to 90 s late) to be answered (45 s of ringing), with room to spare.
export const RING_TTL_MS = 10 * 60 * 1000;
export const RING_SETTLED_BY = new Set(['phone', 'watch']);
export const RING_OUTCOMES = new Set(['answered', 'declined']);

// A stored session is fetched within seconds of the answer; the Watch gives
// up on it after 30 s.
export const RING_SESSION_TTL_MS = 2 * 60 * 1000;
// A sealed start is a call id, an engine and the ring id.
export const RING_START_MAX_CHARS = 1024;
// A sealed session: the Watch's start answer, at most about 60 KB.
export const RING_SESSION_MAX_CHARS = 96 * 1024;
// The longest a fetch waits for the phone's session.
export const RING_SESSION_WAIT_MAX_MS = 20_000;

const ID_PATTERN = /^[A-Za-z0-9_-]{22}$/;
const TOKEN_PATTERN = /^[A-Za-z0-9_-]{43}$/;
const SEALED_PATTERN = /^[A-Za-z0-9_-]+$/;
export const RING_ROUTE = /^\/v1\/rings\/([A-Za-z0-9_-]{22})\/settled$/;
export const RING_SESSION_ROUTE = /^\/v1\/rings\/([A-Za-z0-9_-]{22})\/session$/;

export class Rings {
  constructor({
    maxRings = 10_000,
    ttlMs = RING_TTL_MS,
    maxSessionChars = 32 * 1024 * 1024,
    sessionTtlMs = RING_SESSION_TTL_MS,
    maxWaitersPerRing = 2,
    now = Date.now,
  } = {}) {
    if (!Number.isSafeInteger(maxRings) || maxRings < 1) throw new RangeError('maxRings must be a positive integer');
    this.maxRings = maxRings;
    this.ttlMs = ttlMs;
    this.maxSessionChars = maxSessionChars;
    this.sessionTtlMs = sessionTtlMs;
    this.maxWaitersPerRing = maxWaitersPerRing;
    this.now = now;
    this.rings = new Map();
    // The stored sessions' size, held to maxSessionChars.
    this.sessionChars = 0;
  }

  get size() {
    return this.rings.size;
  }

  // A new ring for this installation: what both pushes carry. The oldest
  // ring goes when the relay holds its maximum (that call only loses the
  // "stop the other device" step).
  create(installationId) {
    this.sweep();
    while (this.rings.size >= this.maxRings) this.drop(this.rings.values().next().value);
    const id = randomBytes(16).toString('base64url');
    const token = randomBytes(32).toString('base64url');
    this.rings.set(id, { id, installationId, tokenHash: digest(token), createdAt: this.now(), settled: null });
    return { id, token };
  }

  // A ring no push carried after all (the call went as a notification).
  forget(id) {
    const ring = this.rings.get(id);
    if (ring) this.drop(ring);
  }

  // { ring } for the first settle, { settled } when the ring was already
  // settled, or {} for an unknown ring or a wrong token (alike, so a guess
  // learns nothing: both hash the token). `start`: the Watch's sealed start,
  // kept only with its answer, for the phone's stop push.
  settle(id, token, by, outcome, start) {
    const ring = this.find(id, token);
    if (!ring) return {};
    if (ring.settled) return { settled: ring.settled };
    ring.settled = { by, outcome };
    if (start && answeredOnWatch(ring)) ring.start = start;
    return { ring };
  }

  // The phone's sealed session for a call the Watch answered: 'stored',
  // 'unknown' (as for a settle), or 'not_answered' when the Watch didn't
  // answer it. A second store replaces the first. The oldest rings' sessions
  // go first when the relay holds its maximum.
  storeSession(id, token, sealed) {
    const ring = this.find(id, token);
    if (!ring) return 'unknown';
    if (!answeredOnWatch(ring)) return 'not_answered';
    this.dropSession(ring);
    for (const other of this.rings.values()) {
      if (this.sessionChars + sealed.length <= this.maxSessionChars) break;
      this.dropSession(other);
    }
    ring.session = { sealed, storedAt: this.now() };
    this.sessionChars += sealed.length;
    for (const wake of [...(ring.waiters ?? [])]) wake(sealed);
    return 'stored';
  }

  // The session for the Watch: { status: 'session', sealed } once stored,
  // waiting up to `waitMs` for it, else { status: 'pending' }. 'unknown' and
  // 'not_answered' as for a store; 'busy' when the ring already has its
  // fetches waiting. `signal` ends a wait whose request went away.
  async takeSession(id, token, { waitMs = 0, signal } = {}) {
    const ring = this.find(id, token);
    if (!ring) return { status: 'unknown' };
    if (!answeredOnWatch(ring)) return { status: 'not_answered' };
    const stored = this.freshSession(ring);
    if (stored) return { status: 'session', sealed: stored };
    if (waitMs <= 0 || signal?.aborted) return { status: 'pending' };
    ring.waiters ??= new Set();
    if (ring.waiters.size >= this.maxWaitersPerRing) return { status: 'busy' };
    return new Promise((resolve) => {
      const done = (sealed) => {
        clearTimeout(timer);
        ring.waiters.delete(done);
        signal?.removeEventListener('abort', ended);
        resolve(sealed ? { status: 'session', sealed } : { status: 'pending' });
      };
      const ended = () => done(null);
      const timer = setTimeout(ended, Math.min(waitMs, RING_SESSION_WAIT_MAX_MS));
      signal?.addEventListener('abort', ended, { once: true });
      ring.waiters.add(done);
    });
  }

  sweep() {
    const cutoff = this.now() - this.ttlMs;
    for (const ring of this.rings.values()) {
      if (ring.createdAt > cutoff) break;
      this.drop(ring);
    }
  }

  // The ring with this id and token, or undefined (an unknown ring and a
  // wrong token alike).
  find(id, token) {
    this.sweep();
    const ring = this.rings.get(id);
    const matches = tokenMatches(token, ring?.tokenHash ?? NO_RING);
    return ring && matches ? ring : undefined;
  }

  freshSession(ring) {
    if (!ring.session) return undefined;
    if (this.now() - ring.session.storedAt < this.sessionTtlMs) return ring.session.sealed;
    this.dropSession(ring);
    return undefined;
  }

  dropSession(ring) {
    if (!ring.session) return;
    this.sessionChars -= ring.session.sealed.length;
    ring.session = undefined;
  }

  // Gone: fetches still waiting on it end without a session.
  drop(ring) {
    this.dropSession(ring);
    this.rings.delete(ring.id);
    for (const wake of [...(ring.waiters ?? [])]) wake(null);
  }
}

// The settle request's fields, or null when any is malformed. A malformed
// `start` is dropped rather than refused: the other device still has to
// stop ringing.
export function validSettle(id, body) {
  if (!ID_PATTERN.test(id) || !body || typeof body !== 'object') return null;
  const { token, by, outcome } = body;
  if (typeof token !== 'string' || !TOKEN_PATTERN.test(token)) return null;
  if (!RING_SETTLED_BY.has(by) || !RING_OUTCOMES.has(outcome)) return null;
  const start = isSealed(body.start, RING_START_MAX_CHARS) ? body.start : undefined;
  return start ? { token, by, outcome, start } : { token, by, outcome };
}

// The phone's session store request, or null when malformed.
export function validSessionStore(id, body) {
  if (!ID_PATTERN.test(id) || !body || typeof body !== 'object') return null;
  const { token, sealed } = body;
  if (typeof token !== 'string' || !TOKEN_PATTERN.test(token)) return null;
  if (!isSealed(sealed, RING_SESSION_MAX_CHARS)) return null;
  return { token, sealed };
}

// The ring token a session fetch carries as its bearer, or null.
export function ringBearer(header) {
  const match = String(header ?? '').match(/^Bearer\s+([A-Za-z0-9_-]{43})$/);
  return match ? match[1] : null;
}

function isSealed(value, maxChars) {
  return typeof value === 'string' && value.length > 0 && value.length <= maxChars && SEALED_PATTERN.test(value);
}

function answeredOnWatch(ring) {
  return ring.settled?.by === 'watch' && ring.settled.outcome === 'answered';
}

// What an unknown ring's token is compared with.
const NO_RING = digest(randomBytes(32));

function digest(value) {
  return createHash('sha256').update(value).digest();
}

function tokenMatches(token, expected) {
  return timingSafeEqual(digest(token), expected);
}
