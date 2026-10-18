import { strict as assert } from 'node:assert';
import { createVerify, generateKeyPairSync, randomBytes } from 'node:crypto';
import { spawn } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { MapsTokens, mapsTokensFromEnv } from '../src/maps-tokens.mjs';

const { privateKey, publicKey } = generateKeyPairSync('ec', { namedCurve: 'P-256' });
const pem = privateKey.export({ type: 'pkcs8', format: 'pem' });

function decode(part) {
  return JSON.parse(Buffer.from(part, 'base64url').toString('utf8'));
}

function fakeApple(answers) {
  const calls = [];
  const fetchImpl = async (url, options) => {
    calls.push({ url, options });
    const next = answers.shift();
    return { ok: next.status === 200, status: next.status, json: async () => next.body };
  };
  return { calls, fetchImpl };
}

test('the JWT is what Apple asks for, signed ES256 with the Maps key', () => {
  const tokens = new MapsTokens({ privateKey: pem, keyId: 'KEY1234567', teamId: 'TEAM123456', now: () => 1_700_000_000_000 });
  const [header, claims, signature] = tokens.authorizationToken().split('.');
  assert.deepEqual(decode(header), { alg: 'ES256', kid: 'KEY1234567', typ: 'JWT' });
  assert.deepEqual(decode(claims), { iss: 'TEAM123456', iat: 1_700_000_000, exp: 1_700_001_800, scope: 'server_api' });
  const verifier = createVerify('SHA256');
  verifier.update(`${header}.${claims}`);
  assert.ok(verifier.verify({ key: publicKey, dsaEncoding: 'ieee-p1363' }, Buffer.from(signature, 'base64url')));
});

test('one access token serves everyone until shortly before it expires', async () => {
  let now = 1_700_000_000_000;
  const apple = fakeApple([
    { status: 200, body: { accessToken: 'first', expiresInSeconds: 1800 } },
    { status: 200, body: { accessToken: 'second', expiresInSeconds: 1800 } },
  ]);
  const tokens = new MapsTokens({ privateKey: pem, keyId: 'K', teamId: 'T', fetchImpl: apple.fetchImpl, now: () => now });
  const [a, b] = await Promise.all([tokens.accessToken(), tokens.accessToken()]);
  assert.equal(a.token, 'first');
  assert.equal(b.token, 'first');
  assert.equal(apple.calls.length, 1, 'concurrent callers share one exchange');
  assert.equal(apple.calls[0].url, 'https://maps-api.apple.com/v1/token');
  assert.match(apple.calls[0].options.headers.Authorization, /^Bearer [\w-]+\.[\w-]+\.[\w-]+$/);
  now += 1000 * 1000;
  assert.deepEqual(await tokens.accessToken(), { token: 'first', expiresIn: 800 });
  now += 700 * 1000;
  assert.equal((await tokens.accessToken()).token, 'second', 'refreshed with under two minutes left');
});

test('a refused or malformed exchange fails without caching', async () => {
  const apple = fakeApple([
    { status: 429, body: {} },
    { status: 200, body: { accessToken: '', expiresInSeconds: 1800 } },
    { status: 200, body: { accessToken: 'ok', expiresInSeconds: 1800 } },
  ]);
  const tokens = new MapsTokens({ privateKey: pem, keyId: 'K', teamId: 'T', fetchImpl: apple.fetchImpl });
  await assert.rejects(tokens.accessToken(), (error) => error.status === 429);
  await assert.rejects(tokens.accessToken(), (error) => error.status === 502);
  assert.equal((await tokens.accessToken()).token, 'ok');
});

test('no key, or an unreadable one, leaves maps off', () => {
  const warnings = [];
  const warn = (message) => warnings.push(message);
  assert.equal(mapsTokensFromEnv({}, { readFile: () => pem, warn }), null);
  const unreadable = () => { throw new Error('EISDIR'); };
  assert.equal(mapsTokensFromEnv({ MAPS_KEY_PATH: '/x', MAPS_KEY_ID: 'K' }, { readFile: unreadable, warn }), null);
  assert.equal(warnings.length, 1);
  assert.ok(mapsTokensFromEnv({ MAPS_KEY_PATH: '/x', MAPS_KEY_ID: 'K', APNS_TEAM_ID: 'T' }, { readFile: () => pem, warn }));
});

async function startRelay(extraEnv) {
  const dir = mkdtempSync(join(tmpdir(), 'conduit-relay-maps-'));
  const port = 23000 + Math.floor(Math.random() * 1000);
  const keyPath = join(dir, 'apns.pem');
  writeFileSync(keyPath, privateKey.export({ type: 'sec1', format: 'pem' }));
  const child = spawn(process.execPath, ['src/server.mjs'], {
    cwd: fileURLToPath(new URL('..', import.meta.url)),
    env: {
      ...process.env, HOST: '127.0.0.1', PORT: String(port), PUBLIC_URL: `https://relay-${port}.example`,
      DATA_PATH: join(dir, 'relay.json'), APNS_KEY_PATH: keyPath, APNS_KEY_ID: 'AAAAAAAAAA', APNS_TEAM_ID: 'BBBBBBBBBB',
      APNS_TOPIC: 'com.milim.relay', APNS_MODE: 'accept', RELAY_TEST_SEAMS: '1', ...extraEnv,
    },
    stdio: 'ignore',
  });
  const base = `http://127.0.0.1:${port}`;
  const deadline = Date.now() + 10_000;
  for (;;) {
    try { if ((await fetch(`${base}/healthz`)).ok) break; } catch {}
    if (Date.now() > deadline) throw new Error('relay did not start');
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  const api = async (path, { method = 'GET', body, credential } = {}) => {
    const response = await fetch(`${base}${path}`, {
      method,
      headers: { ...(body !== undefined ? { 'content-type': 'application/json' } : {}), ...(credential ? { authorization: `Bearer ${credential}` } : {}) },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    return { status: response.status, json: await response.json().catch(() => null) };
  };
  const registered = await api('/v1/installations', { method: 'POST', body: { device_token: randomBytes(32).toString('hex'), bundle_id: 'com.milim.relay', environment: 'production' } });
  const pairing = await api(`/v1/installations/${registered.json.installation.id}/pairings`, { method: 'POST', body: {}, credential: registered.json.credential });
  const claimed = await api('/v1/pairings/claim', { method: 'POST', body: { pairing_code: pairing.json.pairing_code, gateway_name: 'Test Hermes' } });
  return { api, gateway: claimed.json.credential, stop: () => { child.kill('SIGKILL'); rmSync(dir, { recursive: true, force: true }); } };
}

test('a paired host gets a maps token; nobody else does', async () => {
  const relay = await startRelay({ MAPS_MODE: 'fake' });
  try {
    assert.equal((await relay.api('/v1/maps/token', { method: 'POST' })).status, 401);
    const issued = await relay.api('/v1/maps/token', { method: 'POST', credential: relay.gateway });
    assert.equal(issued.status, 200);
    assert.deepEqual(issued.json, { access_token: 'fake-maps-token', expires_in: 1800 });
  } finally {
    relay.stop();
  }
});

test('a relay without a Maps key says so', async () => {
  const relay = await startRelay({});
  try {
    const issued = await relay.api('/v1/maps/token', { method: 'POST', credential: relay.gateway });
    assert.equal(issued.status, 404);
    assert.equal(issued.json.error, 'maps_unavailable');
  } finally {
    relay.stop();
  }
});

test('a maps lookup id rides a plaintext location push like a locate id', async () => {
  const { locationPushFor, validateEvent } = await import('../src/server.mjs');
  const id = `m_${'a'.repeat(20)}`;
  const event = validateEvent({ event_id: 'maps:12345678', type: 'location.requested', profile: 'work', maps: { id } });
  assert.deepEqual(event.maps, { id });
  const push = locationPushFor(event, { id: 'gw' }, { topic: 'com.milim.relay', nowSeconds: 100 });
  assert.deepEqual(push.payload.conduit.maps, { id });
  assert.equal(push.pushType, 'location');
  assert.equal(validateEvent({ event_id: 'maps:87654321', type: 'location.requested', maps: { id: 'nope' } }).maps, undefined);
});
