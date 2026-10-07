import { strict as assert } from 'node:assert';
import { createHash, generateKeyPairSync, randomBytes } from 'node:crypto';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { after, before, test } from 'node:test';

// Watch tool grants against a real relay process: a paired gateway opens a
// grant, a "Watch" posts sealed calls with the grant's relay key, and the
// gateway long-polls and answers. The relay only ever sees opaque strings
// here; the sealing itself is the plugin's and Conduit's (tested there).

const dir = mkdtempSync(join(tmpdir(), 'conduit-relay-watch-'));
const port = 21000 + Math.floor(Math.random() * 1000);
const base = `http://127.0.0.1:${port}`;
const keyPath = join(dir, 'ephemeral-key.pem');
const { privateKey } = generateKeyPairSync('ec', { namedCurve: 'P-256' });
writeFileSync(keyPath, privateKey.export({ type: 'sec1', format: 'pem' }));

const CALL_WAIT_MS = 600;
const HOST_GONE_MS = 1_500;
let child;

async function api(path, { method = 'GET', body, credential } = {}) {
  const response = await fetch(`${base}${path}`, {
    method,
    headers: {
      ...(body !== undefined ? { 'content-type': 'application/json' } : {}),
      ...(credential ? { authorization: `Bearer ${credential}` } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return { status: response.status, json: await response.json().catch(() => null) };
}

before(async () => {
  child = spawn(process.execPath, ['src/server.mjs'], {
    cwd: fileURLToPath(new URL('..', import.meta.url)),
    env: {
      ...process.env,
      HOST: '127.0.0.1',
      PORT: String(port),
      PUBLIC_URL: `https://relay-${port}.example`,
      DATA_PATH: join(dir, 'relay.json'),
      APNS_KEY_PATH: keyPath,
      APNS_KEY_ID: 'AAAAAAAAAA',
      APNS_TEAM_ID: 'BBBBBBBBBB',
      APNS_TOPIC: 'com.milim.relay',
      APNS_MODE: 'accept',
      WATCH_CALL_WAIT_MS: String(CALL_WAIT_MS),
      WATCH_HOST_POLL_WAIT_MS: '400',
      WATCH_HOST_GONE_MS: String(HOST_GONE_MS),
    },
    stdio: 'ignore',
  });
  const deadline = Date.now() + 10_000;
  for (;;) {
    try {
      if ((await fetch(`${base}/healthz`)).ok) break;
    } catch {}
    if (Date.now() > deadline) throw new Error('relay did not start');
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  await pairGateways();
});

after(() => {
  child?.kill('SIGKILL');
  rmSync(dir, { recursive: true, force: true });
});

// One installation with five paired gateways, shared by the tests: the
// relay's own rate limits (registrations per address, pairings per
// installation, grants per gateway per minute) are what they are.
const gateways = {};
let device;

async function pairGateways() {
  const registered = await api('/v1/installations', {
    method: 'POST',
    body: { device_token: randomBytes(32).toString('hex'), bundle_id: 'com.milim.relay', environment: 'production' },
  });
  assert.equal(registered.status, 201);
  device = registered.json.credential;
  const installationId = registered.json.installation.id;
  for (const name of ['a', 'b', 'c', 'd', 'e']) {
    const pairing = await api(`/v1/installations/${installationId}/pairings`, { method: 'POST', body: {}, credential: device });
    assert.equal(pairing.status, 201);
    const claimed = await api('/v1/pairings/claim', { method: 'POST', body: { pairing_code: pairing.json.pairing_code, gateway_name: `Test Hermes ${name}` } });
    assert.equal(claimed.status, 200);
    gateways[name] = claimed.json.credential;
  }
}

function newWatchKey() {
  const key = randomBytes(32).toString('base64url');
  return { key, hash: createHash('sha256').update(key).digest('hex') };
}

async function openGrant(gateway, { ttl = 600, maxCalls = 60 } = {}) {
  const watch = newWatchKey();
  const created = await api('/v1/watch-tools/grants', {
    method: 'POST',
    credential: gateway,
    body: { watch_key_sha256: watch.hash, ttl_s: ttl, max_calls: maxCalls },
  });
  assert.equal(created.status, 201, JSON.stringify(created.json));
  assert.match(created.json.grant_id, /^[A-Za-z0-9_-]{22}$/);
  return { id: created.json.grant_id, watchKey: watch.key, expiresAt: created.json.expires_at };
}

const rid = () => randomBytes(16).toString('base64url');
const sealed = (size = 40) => ({ n: randomBytes(12).toString('base64url'), ct: randomBytes(size).toString('base64url') });

function watchCall(grant, call) {
  return api(`/v1/watch-tools/grants/${grant.id}/calls`, { method: 'POST', credential: grant.watchKey, body: call });
}

function hostPoll(gateway, grant, waitMs = 400) {
  return api(`/v1/watch-tools/grants/${grant.id}/calls?wait_ms=${waitMs}`, { credential: gateway });
}

test('the relay advertises Watch tool grants', async () => {
  const meta = await api('/v1/meta', { credential: device });
  assert.equal(meta.status, 200);
  assert.ok(meta.json.capabilities.includes('watch-tools-v1'));
});

test('a Watch call reaches the host and the host answer reaches the Watch, untouched', async () => {
  const gateway = gateways.a;
  const grant = await openGrant(gateway);
  const call = { rid: rid(), ...sealed() };
  const polled = hostPoll(gateway, grant, 5_000);
  const waiting = watchCall(grant, call);
  const picked = await polled;
  assert.equal(picked.status, 200);
  assert.deepEqual(picked.json.calls, [call]);
  const answer = sealed(300);
  const posted = await api(`/v1/watch-tools/grants/${grant.id}/results/${call.rid}`, { method: 'POST', credential: gateway, body: answer });
  assert.equal(posted.status, 200);
  const answered = await waiting;
  assert.equal(answered.status, 200);
  assert.deepEqual(answered.json, answer);
  // A second answer to the same call has nobody to go to.
  const again = await api(`/v1/watch-tools/grants/${grant.id}/results/${call.rid}`, { method: 'POST', credential: gateway, body: answer });
  assert.equal(again.status, 404);
});

test('a call made before the host polls waits in the queue for the next poll', async () => {
  const gateway = gateways.a;
  const grant = await openGrant(gateway);
  const call = { rid: rid(), ...sealed() };
  const waiting = watchCall(grant, call);
  await new Promise((resolve) => setTimeout(resolve, 100));
  const picked = await hostPoll(gateway, grant);
  assert.deepEqual(picked.json.calls, [call]);
  await api(`/v1/watch-tools/grants/${grant.id}/results/${call.rid}`, { method: 'POST', credential: gateway, body: sealed() });
  assert.equal((await waiting).status, 200);
});

test('only the grant relay key opens a Watch call, and a call id is accepted once', async () => {
  const gateway = gateways.a;
  const grant = await openGrant(gateway);
  const wrongKey = await watchCall({ ...grant, watchKey: newWatchKey().key }, { rid: rid(), ...sealed() });
  assert.equal(wrongKey.status, 401);
  const gatewayKey = await api(`/v1/watch-tools/grants/${grant.id}/calls`, { method: 'POST', credential: gateway, body: { rid: rid(), ...sealed() } });
  assert.equal(gatewayKey.status, 401);
  const unknown = await watchCall({ ...grant, id: rid() }, { rid: rid(), ...sealed() });
  assert.equal(unknown.status, 401);
  const malformed = await watchCall(grant, { rid: 'short', ...sealed() });
  assert.equal(malformed.status, 400);
  const tooBig = await watchCall(grant, { rid: rid(), ...sealed(6_000) });
  assert.equal(tooBig.status, 400);

  const call = { rid: rid(), ...sealed() };
  const first = watchCall(grant, call);
  const duplicate = await watchCall(grant, call);
  assert.equal(duplicate.status, 409);
  assert.equal((await first).status, 504, 'nobody answered within the wait');
});

test('a call nobody answers ends in 504, and the host answer after that finds no caller', async () => {
  const gateway = gateways.a;
  const grant = await openGrant(gateway);
  const call = { rid: rid(), ...sealed() };
  const started = Date.now();
  const answered = await watchCall(grant, call);
  assert.equal(answered.status, 504);
  assert.ok(Date.now() - started >= CALL_WAIT_MS - 50);
  const late = await api(`/v1/watch-tools/grants/${grant.id}/results/${call.rid}`, { method: 'POST', credential: gateway, body: sealed() });
  assert.equal(late.status, 404);
  // Dropped calls aren't handed to the host later either.
  const polled = await hostPoll(gateway, grant, 100);
  assert.deepEqual(polled.json.calls, []);
});

test('a grant whose host stopped polling fails its calls at once', async () => {
  const gateway = gateways.a;
  const grant = await openGrant(gateway);
  await new Promise((resolve) => setTimeout(resolve, HOST_GONE_MS + 200));
  const started = Date.now();
  const answered = await watchCall(grant, { rid: rid(), ...sealed() });
  assert.equal(answered.status, 503);
  assert.equal(answered.json.error, 'host_offline');
  assert.ok(Date.now() - started < CALL_WAIT_MS);
});

test('at most two calls of a grant wait at once, and a grant runs out after its call limit', async () => {
  const gateway = gateways.b;
  const grant = await openGrant(gateway, { maxCalls: 3 });
  const first = watchCall(grant, { rid: rid(), ...sealed() });
  const second = watchCall(grant, { rid: rid(), ...sealed() });
  await new Promise((resolve) => setTimeout(resolve, 100));
  const third = await watchCall(grant, { rid: rid(), ...sealed() });
  assert.equal(third.status, 429);
  assert.equal(third.json.error, 'too_many_calls');
  assert.equal((await first).status, 504);
  assert.equal((await second).status, 504);
  await hostPoll(gateway, grant, 50);
  const fourth = watchCall(grant, { rid: rid(), ...sealed() });
  assert.equal((await fourth).status, 504);
  const fifth = await watchCall(grant, { rid: rid(), ...sealed() });
  assert.equal(fifth.status, 429);
  assert.equal(fifth.json.error, 'grant_exhausted');
});

test('another gateway cannot poll, answer or close a grant', async () => {
  const gateway = gateways.b;
  const other = gateways.c;
  const grant = await openGrant(gateway);
  assert.equal((await hostPoll(other, grant, 50)).status, 410);
  const answer = await api(`/v1/watch-tools/grants/${grant.id}/results/${rid()}`, { method: 'POST', credential: other, body: sealed() });
  assert.equal(answer.status, 410);
  const closed = await api(`/v1/watch-tools/grants/${grant.id}`, { method: 'DELETE', credential: other });
  assert.equal(closed.status, 204);
  assert.equal((await hostPoll(gateway, grant, 50)).status, 200, 'still open for its own gateway');
});

test('the Watch closing its grant ends the host poll and refuses later calls', async () => {
  const gateway = gateways.b;
  const grant = await openGrant(gateway);
  const polled = hostPoll(gateway, grant, 5_000);
  await new Promise((resolve) => setTimeout(resolve, 100));
  const wrongKey = await api(`/v1/watch-tools/grants/${grant.id}`, { method: 'DELETE', credential: newWatchKey().key });
  assert.equal(wrongKey.status, 401);
  const closed = await api(`/v1/watch-tools/grants/${grant.id}`, { method: 'DELETE', credential: grant.watchKey });
  assert.equal(closed.status, 204);
  assert.equal((await polled).status, 410);
  assert.equal((await watchCall(grant, { rid: rid(), ...sealed() })).status, 401);
  // Closing again is a no-op.
  assert.equal((await api(`/v1/watch-tools/grants/${grant.id}`, { method: 'DELETE', credential: grant.watchKey })).status, 204);
});

test('the host closing a grant fails its waiting call', async () => {
  const gateway = gateways.b;
  const grant = await openGrant(gateway);
  const waiting = watchCall(grant, { rid: rid(), ...sealed() });
  await new Promise((resolve) => setTimeout(resolve, 100));
  assert.equal((await api(`/v1/watch-tools/grants/${grant.id}`, { method: 'DELETE', credential: gateway })).status, 204);
  const answered = await waiting;
  assert.equal(answered.status, 410);
});

test('a gateway keeps at most four grants: a fifth closes the oldest', async () => {
  const gateway = gateways.d;
  const grants = [];
  for (let index = 0; index < 5; index += 1) grants.push(await openGrant(gateway));
  assert.equal((await hostPoll(gateway, grants[0], 50)).status, 410);
  for (const grant of grants.slice(1)) assert.equal((await hostPoll(gateway, grant, 50)).status, 200);
});

test('grant requests are checked', async () => {
  const gateway = gateways.e;
  const valid = { watch_key_sha256: newWatchKey().hash, ttl_s: 600, max_calls: 60 };
  assert.equal((await api('/v1/watch-tools/grants', { method: 'POST', body: valid })).status, 401);
  assert.equal((await api('/v1/watch-tools/grants', { method: 'POST', credential: device, body: valid })).status, 401);
  for (const change of [
    { watch_key_sha256: 'abc' },
    { ttl_s: 30 },
    { ttl_s: 1801 },
    { ttl_s: '600' },
    { max_calls: 0 },
    { max_calls: 121 },
  ]) {
    const response = await api('/v1/watch-tools/grants', { method: 'POST', credential: gateway, body: { ...valid, ...change } });
    assert.equal(response.status, 400, JSON.stringify(change));
  }
});
