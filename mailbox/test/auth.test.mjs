import assert from 'node:assert/strict';
import { createHash, generateKeyPairSync, sign } from 'node:crypto';
import test from 'node:test';
import { ReplayCache, inboxIdFor, parseKey, tokenHash, verifyOwnerSignature } from '../src/auth.mjs';

const EMPTY_BODY_SHA256 = '47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU';

function owner() {
  const { publicKey, privateKey } = generateKeyPairSync('ed25519');
  const key = Buffer.from(publicKey.export({ format: 'jwk' }).x, 'base64url');
  return { key, privateKey };
}

// Built by hand from the protocol, not with the module's own helper.
function authorization(privateKey, { method, target, ts, nonce = 'nonce-0123456789ab', body = '' }) {
  const bodyHash = body === '' ? EMPTY_BODY_SHA256 : createHash('sha256').update(body).digest('base64url');
  const message = Buffer.from(`mailbox-v1\n${method}\n${target}\n${ts}\n${nonce}\n${bodyHash}`);
  return `Mailbox-Ed25519 ts=${ts},nonce=${nonce},sig=${sign(null, message, privateKey).toString('base64url')}`;
}

function signatureOf(header) {
  return header.slice(header.indexOf(',sig=') + 5);
}

test('an inbox is named by the hash of its key', () => {
  const key = Buffer.from([...Array(32).keys()]);
  assert.equal(inboxIdFor(key), 'Yw3NKWbEM2aRElRIu7JbT_QSpJxzLbLIq8G4WBvXEN0');
});

test('a key is 32 bytes of unpadded base64url, nothing else', () => {
  assert.deepEqual(parseKey('AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8'), Buffer.from([...Array(32).keys()]));
  for (const bad of ['', 'AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHg', 'AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=',
    'AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh+', 42, null]) {
    assert.equal(parseKey(bad), undefined, String(bad));
  }
});

test('a send token is kept as its hash', () => {
  assert.equal(tokenHash('link-token-for-laifu'), '_net-amN6cIBYbK7AFsbmFd1ydK-Vj6_J7vF2n9GFRA');
});

test('a request signed by the inbox key within five minutes is the owner\'s, kept until its timestamp would be refused', () => {
  const { key, privateKey } = owner();
  const target = '/v1/inboxes/x/envelopes?after=0&wait=25';
  const header = authorization(privateKey, { method: 'GET', target, ts: 1_000_000 });
  const request = { key, method: 'GET', target, body: Buffer.alloc(0), authorization: header };
  const expected = { signature: signatureOf(header), until: 1_000_301 };

  assert.deepEqual(verifyOwnerSignature({ ...request, now: 1_000_299 }), expected);
  assert.deepEqual(verifyOwnerSignature({ ...request, now: 999_700 }), expected, 'sent by a clock five minutes ahead');
});

test('a stale, altered or foreign request is not the owner\'s', () => {
  const { key, privateKey } = owner();
  const other = owner();
  const body = Buffer.from('{"through":7}');
  const target = '/v1/inboxes/x/ack';
  const header = authorization(privateKey, { method: 'POST', target, ts: 1_000_000, body });
  const base = { key, method: 'POST', target, body, authorization: header, now: 1_000_000 };

  assert.equal(verifyOwnerSignature({ ...base, now: 1_000_301 }), undefined, 'too old');
  assert.equal(verifyOwnerSignature({ ...base, now: 999_699 }), undefined, 'too far ahead');
  assert.equal(verifyOwnerSignature({ ...base, body: Buffer.from('{"through":9}') }), undefined, 'another body');
  assert.equal(verifyOwnerSignature({ ...base, target: '/v1/inboxes/y/ack' }), undefined, 'another path');
  assert.equal(verifyOwnerSignature({ ...base, method: 'PUT' }), undefined, 'another method');
  assert.equal(verifyOwnerSignature({ ...base, key: other.key }), undefined, 'another key');
  const signature = signatureOf(header);
  assert.equal(verifyOwnerSignature({ ...base, authorization: header.replace('nonce-0123456789ab', 'nonce-0123456789ac') }), undefined,
    'another nonce');
  const unsigned = createHash('sha256').update(body).digest('base64url');
  const withoutNonce = `Mailbox-Ed25519 ts=1000000,sig=${sign(null, Buffer.from(`mailbox-v1\nPOST\n${target}\n1000000\n${unsigned}`),
    privateKey).toString('base64url')}`;
  for (const bad of [undefined, '', 'Bearer abc', 'Mailbox-Ed25519 ts=1000000', 'Mailbox-Ed25519 sig=abc,ts=1000000', withoutNonce,
    `Mailbox-Ed25519 ts=1000000,nonce=short,sig=${signature}`, `Mailbox-Ed25519 ts=1000000,nonce=${'n'.repeat(65)},sig=${signature}`,
    `Mailbox-Ed25519 ts=1000000,nonce=nonce 0123456789ab,sig=${signature}`]) {
    assert.equal(verifyOwnerSignature({ ...base, authorization: bad }), undefined, String(bad));
  }
  assert.ok(verifyOwnerSignature(base), 'the untouched request');
});

test('an inbox remembers each signature it used until the signature expires', () => {
  const replay = new ReplayCache(2);
  assert.equal(replay.claim('inbox-a', 'sig-1', 1_000, 1_301), true);
  assert.equal(replay.seen('inbox-a', 'sig-1', 1_300), true);
  assert.equal(replay.claim('inbox-a', 'sig-1', 1_300, 1_601), false, 'claimed already');
  assert.equal(replay.seen('inbox-b', 'sig-1', 1_300), false, 'another inbox');
  assert.equal(replay.seen('inbox-a', 'sig-1', 1_301), false, 'expired');
  assert.equal(replay.claim('inbox-a', 'sig-1', 1_301, 1_602), true, 'claimable once expired');
});

test('an inbox out of room is refused, and other inboxes still have theirs', () => {
  const replay = new ReplayCache(2);
  assert.equal(replay.claim('busy', 'sig-1', 1_000, 1_301), true);
  assert.equal(replay.claim('busy', 'sig-2', 1_000, 1_400), true);

  assert.equal(replay.claim('busy', 'sig-3', 1_000, 1_301), false, 'no room');
  assert.equal(replay.claim('quiet', 'sig-3', 1_000, 1_301), true, 'another inbox');
  assert.equal(replay.claim('busy', 'sig-3', 1_301, 1_602), true, 'room once a claim expires');
  assert.equal(replay.claim('busy', 'sig-4', 1_301, 1_602), false, 'the live claims keep their room');
});

test('a sweep forgets expired claims and says how many', () => {
  const replay = new ReplayCache(10);
  replay.claim('inbox-a', 'sig-1', 1_000, 1_301);
  replay.claim('inbox-a', 'sig-2', 1_000, 1_400);
  replay.claim('inbox-b', 'sig-3', 1_000, 1_301);

  assert.equal(replay.sweep(1_301), 2);
  assert.equal(replay.seen('inbox-a', 'sig-2', 1_301), true, 'a live claim stays');
  assert.equal(replay.sweep(1_400), 1);
  assert.equal(replay.sweep(1_400), 0);
});
