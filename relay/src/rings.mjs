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

// Long enough for a push that reached the device late (the phone rings one up
// to 90 s late) to be answered (45 s of ringing), with room to spare.
export const RING_TTL_MS = 10 * 60 * 1000;
export const RING_SETTLED_BY = new Set(['phone', 'watch']);
export const RING_OUTCOMES = new Set(['answered', 'declined']);

const ID_PATTERN = /^[A-Za-z0-9_-]{22}$/;
const TOKEN_PATTERN = /^[A-Za-z0-9_-]{43}$/;
export const RING_ROUTE = /^\/v1\/rings\/([A-Za-z0-9_-]{1,64})\/settled$/;

export class Rings {
  constructor({ maxRings = 10_000, ttlMs = RING_TTL_MS, now = Date.now } = {}) {
    this.maxRings = maxRings;
    this.ttlMs = ttlMs;
    this.now = now;
    this.rings = new Map();
  }

  get size() {
    return this.rings.size;
  }

  // A new ring for this installation: what both pushes carry. The oldest
  // ring goes when the relay holds its maximum (that call only loses the
  // "stop the other device" step).
  create(installationId) {
    this.sweep();
    while (this.rings.size >= this.maxRings) this.rings.delete(this.rings.keys().next().value);
    const id = randomBytes(16).toString('base64url');
    const token = randomBytes(32).toString('base64url');
    this.rings.set(id, { id, installationId, tokenHash: digest(token), createdAt: this.now(), settled: null });
    return { id, token };
  }

  // { ring } for the first settle, { settled } when the ring was already
  // settled, or {} for an unknown ring or a wrong token (alike, so a guess
  // learns nothing).
  settle(id, token, by, outcome) {
    this.sweep();
    const ring = this.rings.get(id);
    if (!ring || !tokenMatches(token, ring.tokenHash)) return {};
    if (ring.settled) return { settled: ring.settled };
    ring.settled = { by, outcome };
    return { ring };
  }

  sweep() {
    const cutoff = this.now() - this.ttlMs;
    for (const [id, ring] of this.rings) {
      if (ring.createdAt > cutoff) break;
      this.rings.delete(id);
    }
  }
}

// The settle request's fields, or null when any is malformed.
export function validSettle(id, body) {
  if (!ID_PATTERN.test(id) || !body || typeof body !== 'object') return null;
  const { token, by, outcome } = body;
  if (typeof token !== 'string' || !TOKEN_PATTERN.test(token)) return null;
  if (!RING_SETTLED_BY.has(by) || !RING_OUTCOMES.has(outcome)) return null;
  return { token, by, outcome };
}

function digest(value) {
  return createHash('sha256').update(value).digest();
}

function tokenMatches(token, expected) {
  return timingSafeEqual(digest(token), expected);
}
