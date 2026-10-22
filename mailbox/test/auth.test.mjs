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
function authorization(privateKey, { method, target, ts, body = '' }) {
  const bodyHash = body === '' ? EMPTY_BODY_SHA256 : createHash('sha256').update(body).digest('base64url');
  const message = Buffer.from(`mailbox-v1\n${method}\n${target}\n${ts}\n${bodyHash}`);
  return `Mailbox-Ed25519 ts=${ts},sig=${sign(null, message, privateKey).toString('base64url')}`;
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
  for (const bad of [undefined, '', 'Bearer abc', 'Mailbox-Ed25519 ts=1000000', 'Mailbox-Ed25519 sig=abc,ts=1000000']) {
    assert.equal(verifyOwnerRequest({ ...base, authorization: bad, now: 1_000_000, replay: new ReplayCache() }), false, String(bad));
  }
  assert.equal(verifyOwnerRequest({ ...base, now: 1_000_000, replay: new ReplayCache() }), true, 'the untouched request');
});

test('the replay cache forgets a signature once it could no longer be fresh', () => {
  const replay = new ReplayCache(600);
  assert.equal(replay.claim('sig-a', 1_000), true);
  assert.equal(replay.claim('sig-a', 1_599), false);
  assert.equal(replay.claim('sig-a', 1_601), true);
});
