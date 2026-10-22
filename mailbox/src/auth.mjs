import { createHash, createPublicKey, verify } from 'node:crypto';

// How far an owner request's timestamp may be from the mailbox's clock.
export const SIGNATURE_WINDOW_SECONDS = 300;
const KEY_PATTERN = /^[A-Za-z0-9_-]{43}$/;
const AUTHORIZATION_PATTERN = /^Mailbox-Ed25519 ts=(\d{1,12}),nonce=([A-Za-z0-9_-]{16,64}),sig=([A-Za-z0-9_-]{86})$/;

export function sha256(bytes) {
  return createHash('sha256').update(bytes).digest('base64url');
}

// An inbox is addressed by the hash of its owner's Ed25519 public key.
export function inboxIdFor(key) {
  return sha256(key);
}

// The raw 32-byte public key, from unpadded base64url; undefined otherwise.
export function parseKey(value) {
  if (typeof value !== 'string' || !KEY_PATTERN.test(value)) return undefined;
  const key = Buffer.from(value, 'base64url');
  return key.length === 32 && key.toString('base64url') === value ? key : undefined;
}

export function tokenHash(token) {
  return sha256(Buffer.from(token, 'utf8'));
}

// What an owner signs: the method, the request target as sent (path and
// query), the timestamp, a nonce and the body's hash, so none can be
// swapped. Ed25519 signs the same bytes the same way, so the nonce is what
// lets an owner send one request twice in a second (a retry, a second poll).
export function signedString({ method, target, ts, nonce, body }) {
  return Buffer.from(`mailbox-v1\n${method}\n${target}\n${ts}\n${nonce}\n${sha256(body)}`, 'utf8');
}

export function verifyOwnerRequest({ key, method, target, body, authorization, now, replay }) {
  const match = AUTHORIZATION_PATTERN.exec(typeof authorization === 'string' ? authorization : '');
  if (!match) return false;
  const ts = Number(match[1]);
  if (Math.abs(now - ts) > SIGNATURE_WINDOW_SECONDS) return false;
  let valid;
  try {
    const publicKey = createPublicKey({ key: { kty: 'OKP', crv: 'Ed25519', x: key.toString('base64url') }, format: 'jwk' });
    valid = verify(null, signedString({ method, target, ts: match[1], nonce: match[2], body }), publicKey, Buffer.from(match[3], 'base64url'));
  } catch {
    return false;
  }
  // Checked last, so a forged signature can't use up a real one's slot. Kept
  // until the timestamp itself would be refused.
  return valid && replay.claim(match[3], now, ts + SIGNATURE_WINDOW_SECONDS + 1);
}

// Signatures still inside their window, so a captured request can't be sent
// again. Past `maxEntries` live signatures it refuses new ones until some
// age out, and says it is `full` so the server can answer busy, not 401.
export class ReplayCache {
  constructor(maxEntries = 100_000) {
    this.maxEntries = maxEntries;
    this.seen = new Map();
    this.sweptAt = 0;
  }

  get full() {
    return this.seen.size >= this.maxEntries;
  }

  // Claims `signature` until `expiresAt`; false if it is claimed already or
  // there is no room.
  claim(signature, now, expiresAt) {
    const until = this.seen.get(signature);
    if (until !== undefined && until > now) return false;
    if (this.seen.size >= this.maxEntries / 2 && now - this.sweptAt >= 10) {
      this.sweptAt = now;
      for (const [seen, before] of this.seen) if (before <= now) this.seen.delete(seen);
    }
    if (until === undefined && this.full) return false;
    this.seen.set(signature, expiresAt);
    return true;
  }
}
