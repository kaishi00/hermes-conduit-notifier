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

// The signature, when `authorization` signs this request with `key` within
// the window, and until when it must be remembered: past that, its
// timestamp is refused anyway. Undefined otherwise. Whether the signature
// was used before is the ReplayCache's to say.
export function verifyOwnerSignature({ key, method, target, body, authorization, now }) {
  const match = AUTHORIZATION_PATTERN.exec(typeof authorization === 'string' ? authorization : '');
  if (!match) return undefined;
  const ts = Number(match[1]);
  if (Math.abs(now - ts) > SIGNATURE_WINDOW_SECONDS) return undefined;
  try {
    const publicKey = createPublicKey({ key: { kty: 'OKP', crv: 'Ed25519', x: key.toString('base64url') }, format: 'jwk' });
    const message = signedString({ method, target, ts: match[1], nonce: match[2], body });
    if (!verify(null, message, publicKey, Buffer.from(match[3], 'base64url'))) return undefined;
  } catch {
    return undefined;
  }
  return { signature: match[3], until: ts + SIGNATURE_WINDOW_SECONDS + 1 };
}

// The signatures each inbox has used while they could still pass, so a
// captured request can't be sent again. Every inbox has its own room of
// `maxPerInbox`: one that runs out is refused until its claims expire,
// and no other inbox notices.
export class ReplayCache {
  constructor(maxPerInbox = 7_200) {
    this.maxPerInbox = maxPerInbox;
    this.inboxes = new Map();
  }

  seen(inbox, signature, now) {
    const until = this.inboxes.get(inbox)?.claims.get(signature);
    return until !== undefined && until > now;
  }

  // Claims `signature` for `inbox` until `until`; false if it is claimed
  // already or the inbox has no room left.
  claim(inbox, signature, now, until) {
    if (this.seen(inbox, signature, now)) return false;
    let room = this.inboxes.get(inbox);
    if (!room) this.inboxes.set(inbox, (room = { claims: new Map(), sweptAt: 0 }));
    // Claims expire by the second, so a full room is swept once a second at most.
    if (room.claims.size >= this.maxPerInbox && room.sweptAt !== now) {
      room.sweptAt = now;
      forgetExpired(room.claims, now);
    }
    if (room.claims.size >= this.maxPerInbox) return false;
    room.claims.set(signature, until);
    return true;
  }

  // Forgets expired claims, and inboxes left with none; how many it forgot.
  sweep(now) {
    let forgotten = 0;
    for (const [inbox, room] of this.inboxes) {
      forgotten += forgetExpired(room.claims, now);
      if (!room.claims.size) this.inboxes.delete(inbox);
    }
    return forgotten;
  }
}

function forgetExpired(claims, now) {
  let forgotten = 0;
  for (const [signature, until] of claims) {
    if (until > now) continue;
    claims.delete(signature);
    forgotten += 1;
  }
  return forgotten;
}
