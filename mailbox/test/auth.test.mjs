import assert from 'node:assert/strict';
import { createHash, generateKeyPairSync, sign } from 'node:crypto';
import test from 'node:test';
import { ReplayCache, inboxIdFor, parseKey, tokenHash, verifyOwnerRequest } from '../src/auth.mjs';

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

test('a request signed by the inbox key within five minutes is the owner\'s, once', () => {
  const { key, privateKey } = owner();
  const replay = new ReplayCache();
  const target = '/v1/inboxes/x/envelopes?after=0&wait=25';
  const header = authorization(privateKey, { method: 'GET', target, ts: 1_000_000 });
  const request = { key, method: 'GET', target, body: Buffer.alloc(0), authorization: header, replay };

  assert.equal(verifyOwnerRequest({ ...request, now: 1_000_299 }), true);
  assert.equal(verifyOwnerRequest({ ...request, now: 1_000_299 }), false, 'a replayed request');
});

test('the same request sent twice in one second is two requests, told apart by their nonces', () => {
  const { key, privateKey } = owner();
  const replay = new ReplayCache();
  const target = '/v1/inboxes/x/envelopes?after=0&wait=25';
  const request = { key, method: 'GET', target, body: Buffer.alloc(0), now: 1_000_000, replay };

  for (const nonce of ['first-poll-nonce-0001', 'second-poll-nonce-002']) {
    const header = authorization(privateKey, { method: 'GET', target, ts: 1_000_000, nonce });
    assert.equal(verifyOwnerRequest({ ...request, authorization: header }), true, nonce);
  }
});

test('a stale, altered or foreign request is not the owner\'s', () => {
  const { key, privateKey } = owner();
  const other = owner();
  const body = Buffer.from('{"through":7}');
  const target = '/v1/inboxes/x/ack';
  const header = authorization(privateKey, { method: 'POST', target, ts: 1_000_000, body });
  const base = { key, method: 'POST', target, body, authorization: header };

  assert.equal(verifyOwnerRequest({ ...base, now: 1_000_301, replay: new ReplayCache() }), false, 'too old');
  assert.equal(verifyOwnerRequest({ ...base, now: 999_699, replay: new ReplayCache() }), false, 'too far ahead');
  assert.equal(verifyOwnerRequest({ ...base, body: Buffer.from('{"through":9}'), now: 1_000_000, replay: new ReplayCache() }), false, 'another body');
  assert.equal(verifyOwnerRequest({ ...base, target: '/v1/inboxes/y/ack', now: 1_000_000, replay: new ReplayCache() }), false, 'another path');
  assert.equal(verifyOwnerRequest({ ...base, method: 'PUT', now: 1_000_000, replay: new ReplayCache() }), false, 'another method');
  assert.equal(verifyOwnerRequest({ ...base, key: other.key, now: 1_000_000, replay: new ReplayCache() }), false, 'another key');
  const signature = header.slice(header.indexOf(',sig=') + 5);
  assert.equal(verifyOwnerRequest({ ...base, authorization: header.replace('nonce-0123456789ab', 'nonce-0123456789ac'), now: 1_000_000,
    replay: new ReplayCache() }), false, 'another nonce');
  const unsigned = createHash('sha256').update(body).digest('base64url');
  const withoutNonce = `Mailbox-Ed25519 ts=1000000,sig=${sign(null, Buffer.from(`mailbox-v1\nPOST\n${target}\n1000000\n${unsigned}`),
    privateKey).toString('base64url')}`;
  for (const bad of [undefined, '', 'Bearer abc', 'Mailbox-Ed25519 ts=1000000', 'Mailbox-Ed25519 sig=abc,ts=1000000', withoutNonce,
    `Mailbox-Ed25519 ts=1000000,nonce=short,sig=${signature}`, `Mailbox-Ed25519 ts=1000000,nonce=${'n'.repeat(65)},sig=${signature}`,
    `Mailbox-Ed25519 ts=1000000,nonce=nonce 0123456789ab,sig=${signature}`]) {
    assert.equal(verifyOwnerRequest({ ...base, authorization: bad, now: 1_000_000, replay: new ReplayCache() }), false, String(bad));
  }
  assert.equal(verifyOwnerRequest({ ...base, now: 1_000_000, replay: new ReplayCache() }), true, 'the untouched request');
});

test('a signature is refused again until its timestamp is too old to pass', () => {
  const { key, privateKey } = owner();
  const replay = new ReplayCache();
  const target = '/v1/inboxes/x/envelopes';
  const header = authorization(privateKey, { method: 'GET', target, ts: 1_000_000 });
  const request = { key, method: 'GET', target, body: Buffer.alloc(0), authorization: header, replay };

  assert.equal(verifyOwnerRequest({ ...request, now: 999_700 }), true, 'sent by a clock five minutes ahead');
  assert.equal(verifyOwnerRequest({ ...request, now: 1_000_300 }), false, 'the last second it would pass');
});

test('the replay cache forgets a claim once it expires, and says when it is full', () => {
  const replay = new ReplayCache(2);
  assert.equal(replay.claim('sig-a', 1_000, 1_301), true);
  assert.equal(replay.claim('sig-a', 1_300, 1_601), false);
  assert.equal(replay.claim('sig-b', 1_000, 1_301), true);

  assert.equal(replay.full, true);
  assert.equal(replay.claim('sig-c', 1_000, 1_301), false, 'no room');
  assert.equal(replay.claim('sig-a', 1_301, 1_602), true, 'expired');
  assert.equal(replay.full, false);
});
