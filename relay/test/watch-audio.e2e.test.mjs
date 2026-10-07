import { strict as assert } from 'node:assert';
import { createHash, generateKeyPairSync, randomBytes } from 'node:crypto';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { request as httpRequest } from 'node:http';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { after, before, test } from 'node:test';

// Live Watch audio against a real relay process: a paired gateway opens an
// audio grant and dials in as the host, a "Watch" dials in with the grant's
// relay key, and binary messages cross untouched both ways. Node's own
// WebSocket client stands in for both ends. The sealing is the plugin's and
// Conduit's (tested there): the relay only ever sees opaque bytes.

const dir = mkdtempSync(join(tmpdir(), 'conduit-relay-audio-'));
const port = 22000 + Math.floor(Math.random() * 1000);
const base = `http://127.0.0.1:${port}`;
const wsBase = `ws://127.0.0.1:${port}`;
const keyPath = join(dir, 'ephemeral-key.pem');
const { privateKey } = generateKeyPairSync('ec', { namedCurve: 'P-256' });
writeFileSync(keyPath, privateKey.export({ type: 'sec1', format: 'pem' }));
const IDLE_MS = 1_500;
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
      WATCH_AUDIO_IDLE_MS: String(IDLE_MS),
      RELAY_MAX_WATCH_AUDIO_BRIDGES_PER_GATEWAY: '2',
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
  for (const name of ['a', 'b', 'c', 'd']) {
    const pairing = await api(`/v1/installations/${installationId}/pairings`, { method: 'POST', body: {}, credential: device });
    assert.equal(pairing.status, 201);
    const claimed = await api('/v1/pairings/claim', { method: 'POST', body: { pairing_code: pairing.json.pairing_code, gateway_name: `Test Hermes ${name}` } });
    assert.equal(claimed.status, 200);
    gateways[name] = claimed.json.credential;
  }
}

async function openGrant(gateway, { audio = true } = {}) {
  const key = randomBytes(32).toString('base64url');
  const created = await api('/v1/watch-tools/grants', {
    method: 'POST',
    credential: gateway,
    body: { watch_key_sha256: createHash('sha256').update(key).digest('hex'), ttl_s: 600, max_calls: 60, audio },
  });
  assert.equal(created.status, 201, JSON.stringify(created.json));
  assert.equal(created.json.audio, audio);
  return { id: created.json.grant_id, watchKey: key };
}

// A client socket that records what it receives and how it closed.
function connect(path, credential) {
  const socket = new WebSocket(`${wsBase}${path}`, credential ? { headers: { authorization: `Bearer ${credential}` } } : undefined);
  socket.binaryType = 'arraybuffer';
  const client = { socket, messages: [], closed: null };
  client.opened = new Promise((resolve, reject) => {
    socket.addEventListener('open', () => resolve(), { once: true });
    socket.addEventListener('error', () => reject(new Error('socket error')), { once: true });
  });
  client.closing = new Promise((resolve) => socket.addEventListener('close', (event) => {
    client.closed = { code: event.code, reason: event.reason };
    resolve(client.closed);
  }, { once: true }));
  client.next = (count = 1) => new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`timed out waiting for ${count} messages, have ${client.messages.length}`)), 3_000);
    const check = () => {
      if (client.messages.length >= count) {
        clearTimeout(timer);
        socket.removeEventListener('message', check);
        resolve(client.messages.splice(0, count));
      }
    };
    socket.addEventListener('message', check);
    check();
  });
  socket.addEventListener('message', (event) => client.messages.push(Buffer.from(event.data)));
  return client;
}

const hostPath = (grant) => `/v1/watch-audio/${grant.id}/host`;
const watchPath = (grant) => `/v1/watch-audio/${grant.id}/watch`;
const notice = (n) => Buffer.from([0, n]);
const sealedFrame = (size = 600) => Buffer.concat([Buffer.from([1]), randomBytes(size)]);

// The HTTP status of a refused upgrade (or 101).
function upgradeStatus(path, credential) {
  return new Promise((resolve, reject) => {
    const request = httpRequest(`${base}${path}`, {
      headers: {
        connection: 'Upgrade',
        upgrade: 'websocket',
        'sec-websocket-version': '13',
        'sec-websocket-key': randomBytes(16).toString('base64'),
        ...(credential ? { authorization: `Bearer ${credential}` } : {}),
      },
    });
    request.on('upgrade', (response, socket) => {
      socket.destroy();
      resolve(response.statusCode);
    });
    request.on('response', (response) => {
      response.resume();
      resolve(response.statusCode);
    });
    request.on('error', reject);
    request.end();
  });
}

async function openBridge(gateway) {
  const grant = await openGrant(gateway);
  const host = connect(hostPath(grant), gateway);
  await host.opened;
  const watch = connect(watchPath(grant), grant.watchKey);
  await watch.opened;
  assert.deepEqual(await host.next(), [notice(1)]);
  return { grant, host, watch };
}

test('the relay advertises Watch audio', async () => {
  const meta = await api('/v1/meta', { credential: device });
  assert.ok(meta.json.capabilities.includes('watch-audio-v1'));
});

test('messages cross the bridge untouched in both directions', async () => {
  const { host, watch } = await openBridge(gateways.a);
  const up = [sealedFrame(640), sealedFrame(40_000), sealedFrame(1)];
  for (const message of up) watch.socket.send(message);
  assert.deepEqual(await host.next(3), up);
  const down = [sealedFrame(960), Buffer.concat([Buffer.from([2]), Buffer.from('{"type":"turn.done"}')])];
  for (const message of down) host.socket.send(message);
  assert.deepEqual(await watch.next(2), down);
  watch.socket.close(1000);
  host.socket.close(1000);
  await Promise.all([watch.closing, host.closing]);
});

test('a grant without audio, a wrong key or another gateway cannot open the bridge', async () => {
  const plain = await openGrant(gateways.b, { audio: false });
  assert.equal(await upgradeStatus(hostPath(plain), gateways.b), 403);
  const grant = await openGrant(gateways.b);
  assert.equal(await upgradeStatus(hostPath(grant), gateways.c), 410);
  assert.equal(await upgradeStatus(hostPath(grant)), 401);
  assert.equal(await upgradeStatus(watchPath(grant), randomBytes(32).toString('base64url')), 401);
  assert.equal(await upgradeStatus(watchPath(grant)), 401);
  assert.equal(await upgradeStatus(`/v1/watch-audio/${grant.id}/elsewhere`, gateways.b), 404);
  assert.equal(await upgradeStatus('/v1/meta', device), 404);
});

test('a Watch that arrives before its host is turned away, never queued', async () => {
  const grant = await openGrant(gateways.b);
  const watch = connect(watchPath(grant), grant.watchKey);
  assert.equal((await watch.closing).code, 4503);
});

test('a newer Watch socket replaces the older one and the host hears about it', async () => {
  const { grant, host, watch } = await openBridge(gateways.c);
  const second = connect(watchPath(grant), grant.watchKey);
  await second.opened;
  assert.equal((await watch.closing).code, 4000);
  assert.deepEqual(await host.next(2), [notice(2), notice(1)]);
  second.socket.send(sealedFrame());
  assert.equal((await host.next())[0].length, 601);
  second.socket.close(1000);
  assert.deepEqual(await host.next(), [notice(2)]);
  host.socket.close(1000);
});

test('the Watch is cut off when its host leaves', async () => {
  const { host, watch } = await openBridge(gateways.c);
  host.socket.close(1000);
  assert.equal((await watch.closing).code, 4503);
});

test('closing the grant ends the bridge on both sides', async () => {
  const { grant, host, watch } = await openBridge(gateways.d);
  const deleted = await fetch(`${base}/v1/watch-tools/grants/${grant.id}`, { method: 'DELETE', headers: { authorization: `Bearer ${gateways.d}` } });
  assert.equal(deleted.status, 204);
  assert.equal((await watch.closing).code, 4010);
  assert.equal((await host.closing).code, 4010);
});

test('only the relay sends notices', async () => {
  const { host, watch } = await openBridge(gateways.d);
  watch.socket.send(Buffer.from([0, 1]));
  assert.equal((await watch.closing).code, 1002);
  assert.deepEqual(await host.next(), [notice(2)]);
  host.socket.close(1000);
});

test('an oversized message and a flood are cut off', async () => {
  const first = await openBridge(gateways.a);
  first.watch.socket.send(sealedFrame(64 * 1024));
  assert.equal((await first.watch.closing).code, 1009);
  first.host.socket.close(1000);

  const second = await openBridge(gateways.a);
  for (let i = 0; i < 9; i += 1) second.watch.socket.send(sealedFrame(60_000));
  assert.equal((await second.watch.closing).code, 4429);
  second.host.socket.close(1000);
});

test('a silent socket is closed as idle', async () => {
  const { host, watch } = await openBridge(gateways.b);
  // The host pings, so only the Watch goes quiet.
  const pinger = setInterval(() => host.socket.send(sealedFrame(1)), 300);
  try {
    assert.equal((await watch.closing).code, 4408);
  } finally {
    clearInterval(pinger);
    host.socket.close(1000);
  }
});

test('a gateway holds at most two bridges at once', async () => {
  const opened = [];
  for (let i = 0; i < 2; i += 1) {
    const grant = await openGrant(gateways.d);
    const host = connect(hostPath(grant), gateways.d);
    await host.opened;
    opened.push(host);
  }
  const third = await openGrant(gateways.d);
  assert.equal(await upgradeStatus(hostPath(third), gateways.d), 503);
  for (const host of opened) host.socket.close(1000);
  await Promise.all(opened.map((host) => host.closing));
  await new Promise((resolve) => setTimeout(resolve, 100));
  const host = connect(hostPath(third), gateways.d);
  await host.opened;
  host.socket.close(1000);
});
