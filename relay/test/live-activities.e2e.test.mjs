import { strict as assert } from 'node:assert';
import { generateKeyPairSync } from 'node:crypto';
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { createServer as createNetServer } from 'node:net';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { after, before, test } from 'node:test';
import { RelayStore } from '../src/store.mjs';

// Lock Screen progress (#605) through a real relay process: the phone
// registers its activity token, the host reports progress, and the captured
// APNs request is a liveactivity push on the app's liveactivity topic.

const dir = mkdtempSync(join(tmpdir(), 'conduit-relay-live-'));
const dataPath = join(dir, 'relay-data.json');
const capturePath = join(dir, 'apns-capture.jsonl');
const keyPath = join(dir, 'key.pem');
const { privateKey } = generateKeyPairSync('ec', { namedCurve: 'P-256' });
writeFileSync(keyPath, privateKey.export({ type: 'sec1', format: 'pem' }));

let child;
let base;
let device;
let host;
let installationId;
const TOKEN = 'ab'.repeat(40);

function freePort() {
  return new Promise((resolve, reject) => {
    const probe = createNetServer();
    probe.listen(0, '127.0.0.1', () => {
      const { port } = probe.address();
      probe.close(() => resolve(port));
    });
    probe.on('error', reject);
  });
}

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

function captured() {
  if (!existsSync(capturePath)) return [];
  return readFileSync(capturePath, 'utf8').split('\n').filter(Boolean).map((line) => JSON.parse(line));
}

async function waitForCaptures(count) {
  const deadline = Date.now() + 5_000;
  while (captured().length < count) {
    if (Date.now() > deadline) throw new Error(`expected ${count} pushes, got ${captured().length}`);
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  return captured();
}

before(async () => {
  const fixture = new RelayStore(dataPath);
  const created = fixture.createInstallation({ bundleId: 'com.milim.relay', deviceToken: 'c'.repeat(64), environment: 'production' });
  const pairing = fixture.createPairing(created.installation.id);
  const claim = fixture.claimPairing(pairing.code, 'live host');
  installationId = created.installation.id;
  device = `${installationId}.${created.deviceSecret}`;
  host = `${installationId}.${claim.gatewayId}.${claim.gatewaySecret}`;
  const port = await freePort();
  base = `http://127.0.0.1:${port}`;
  child = spawn(process.execPath, ['src/server.mjs'], {
    cwd: fileURLToPath(new URL('..', import.meta.url)),
    env: {
      ...process.env,
      HOST: '127.0.0.1',
      PORT: String(port),
      PUBLIC_URL: `https://relay-${port}.example`,
      DATA_PATH: dataPath,
      APNS_KEY_PATH: keyPath,
      APNS_KEY_ID: 'AAAAAAAAAA',
      APNS_TEAM_ID: 'BBBBBBBBBB',
      APNS_TOPIC: 'com.milim.relay',
      APNS_MODE: 'accept',
      RELAY_TEST_SEAMS: '1',
      APNS_CAPTURE_PATH: capturePath,
    },
    stdio: 'ignore',
  });
  const deadline = Date.now() + 10_000;
  for (;;) {
    try {
      if ((await fetch(`${base}/healthz`)).ok) break;
    } catch {}
    if (Date.now() > deadline) throw new Error('relay did not start');
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
});

after(async () => {
  if (child && child.exitCode === null) {
    child.kill('SIGTERM');
    await new Promise((resolve) => child.once('exit', resolve));
  }
  rmSync(dir, { recursive: true, force: true });
});

test('the relay says it carries Live Activities', async () => {
  const meta = await api('/v1/meta', { credential: device });
  assert.equal(meta.status, 200);
  assert.ok(meta.json.capabilities.includes('live-activities'));
});

test('a watched turn reaches the phone as liveactivity pushes, and its end ends it', async () => {
  const report = (body) => api('/v1/live-activity', { method: 'POST', credential: host, body });
  assert.deepEqual((await report({ session_id: 'stored-1', state: { phase: 'thinking' } })).json, { tracked: false },
    'nothing registered yet');
  assert.equal(captured().length, 0);

  const registered = await api(`/v1/installations/${installationId}/live-activities`, {
    method: 'PUT', credential: device, body: { push_token: TOKEN, session_ids: ['runtime-1', 'stored-1'] },
  });
  assert.equal(registered.status, 200);

  assert.deepEqual((await report({ session_id: 'stored-1', state: { phase: 'tool', tool: 'terminal', tools: 1, context: 20 } })).json,
    { tracked: true });
  const [update] = await waitForCaptures(1);
  assert.equal(update.deviceToken, TOKEN);
  assert.equal(update.notification.pushType, 'liveactivity');
  assert.equal(update.notification.topic, 'com.milim.relay.push-type.liveactivity');
  assert.deepEqual(update.notification.payload.aps['content-state'], { phase: 'tool', tools: 1, tool: 'terminal', context: 20 });

  assert.deepEqual((await report({ session_id: 'stored-1', state: { phase: 'thinking', tools: 1 }, end: 'done' })).json,
    { tracked: true });
  const pushes = await waitForCaptures(2);
  assert.equal(pushes[1].notification.payload.aps.event, 'end');
  assert.deepEqual((await report({ session_id: 'stored-1', state: { phase: 'thinking' } })).json, { tracked: false },
    'the ended activity is forgotten');
});

test('the phone removes an activity; the host then hears nobody is watching', async () => {
  await api(`/v1/installations/${installationId}/live-activities`, {
    method: 'PUT', credential: device, body: { push_token: TOKEN, session_ids: ['stored-2'] },
  });
  const removed = await api(`/v1/installations/${installationId}/live-activities`, {
    method: 'DELETE', credential: device, body: { push_token: TOKEN },
  });
  assert.equal(removed.status, 204);
  const answer = await api('/v1/live-activity', { method: 'POST', credential: host, body: { session_id: 'stored-2', state: { phase: 'thinking' } } });
  assert.deepEqual(answer.json, { tracked: false });
});

test('malformed or unauthenticated requests are refused', async () => {
  assert.equal((await api('/v1/live-activity', { method: 'POST', body: { session_id: 's', state: { phase: 'thinking' } } })).status, 401);
  assert.equal((await api('/v1/live-activity', { method: 'POST', credential: device, body: { session_id: 's', state: { phase: 'thinking' } } })).status, 401,
    'a phone credential is not a host credential');
  assert.equal((await api('/v1/live-activity', { method: 'POST', credential: host, body: { session_id: 's', state: { phase: 'tool', tool: 'secret-tool-name' } } })).status, 400);
  assert.equal((await api(`/v1/installations/${installationId}/live-activities`, { method: 'PUT', body: { push_token: TOKEN, session_ids: ['s'] } })).status, 401);
  assert.equal((await api(`/v1/installations/${installationId}/live-activities`, { method: 'PUT', credential: device, body: { push_token: 'nope', session_ids: ['s'] } })).status, 400);
});
