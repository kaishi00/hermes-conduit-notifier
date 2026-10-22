import { createHash, createPublicKey, verify } from 'node:crypto';

// How far an owner request's timestamp may be from the mailbox's clock.
export const SIGNATURE_WINDOW_SECONDS = 300;
const KEY_PATTERN = /^[A-Za-z0-9_-]{43}$/;
const AUTHORIZATION_PATTERN = /^Mailbox-Ed25519 ts=(\d{1,12}),sig=([A-Za-z0-9_-]{86})$/;

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
// query), the timestamp and the body's hash, so none can be swapped.
export function signedString({ method, target, ts, body }) {
  return Buffer.from(`mailbox-v1\n${method}\n${target}\n${ts}\n${sha256(body)}`, 'utf8');
}

export function verifyOwnerRequest({ key, method, target, body, authorization, now, replay }) {
  const match = AUTHORIZATION_PATTERN.exec(typeof authorization === 'string' ? authorization : '');
  if (!match) return false;
  const ts = Number(match[1]);
  if (Math.abs(now - ts) > SIGNATURE_WINDOW_SECONDS) return false;
  let valid;
  try {
    const publicKey = createPublicKey({ key: { kty: 'OKP', crv: 'Ed25519', x: key.toString('base64url') }, format: 'jwk' });
    valid = verify(null, signedString({ method, target, ts: match[1], body }), publicKey, Buffer.from(match[2], 'base64url'));
  } catch {
    return false;
  }
  // Checked last, so a forged signature can't use up a real one's slot.
  return valid && replay.claim(match[2], now);
}

// Signatures seen within the window, so a captured request can't be sent
// again. Past `maxEntries` live signatures it refuses new ones until some
// age out: an owner retries, a flood doesn't grow the map.
export class ReplayCache {
  constructor(ttlSeconds = SIGNATURE_WINDOW_SECONDS * 2, maxEntries = 100_000) {
    this.ttlSeconds = ttlSeconds;
    this.maxEntries = maxEntries;
    this.seen = new Map();
    this.sweptAt = 0;
  }

  claim(signature, now) {
    const expiresAt = this.seen.get(signature);
    if (expiresAt !== undefined && expiresAt > now) return false;
    if (this.seen.size >= this.maxEntries / 2 && now - this.sweptAt >= 10) {
      this.sweptAt = now;
      for (const [seen, until] of this.seen) if (until <= now) this.seen.delete(seen);
    }
    if (expiresAt === undefined && this.seen.size >= this.maxEntries) return false;
    this.seen.set(signature, now + this.ttlSeconds);
    return true;
  }
}
