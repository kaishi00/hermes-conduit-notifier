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

// Registration proof (#142, #146) through a real relay process: a phone
// registers, or moves to a new device token, only with the nonce the relay
// pushed to that token, and can't take another phone's PushKit token.

const dir = mkdtempSync(join(tmpdir(), 'conduit-relay-proof-'));
const dataPath = join(dir, 'relay-data.json');
const capturePath = join(dir, 'apns-capture.jsonl');
const keyPath = join(dir, 'key.pem');
const { privateKey } = generateKeyPairSync('ec', { namedCurve: 'P-256' });
writeFileSync(keyPath, privateKey.export({ type: 'sec1', format: 'pem' }));

const VICTIM_TOKEN = '1'.repeat(64);
const VICTIM_VOIP = '2'.repeat(64);
let child;
let base;
let victim;

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

// Each request comes from its own address, so only the per-token limits
// meet across tests.
let address = 0;

async function api(path, { method = 'GET', body, credential } = {}) {
  address += 1;
  const response = await fetch(`${base}${path}`, {
    method,
    headers: {
      'x-forwarded-for': `10.0.${address >> 8}.${address & 255}`,
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

const registration = (deviceToken, extra = {}) => ({ bundle_id: 'com.milim.relay', environment: 'production', device_token: deviceToken, ...extra });

// Asks for a challenge and returns the nonce pushed to the token.
async function challenge(deviceToken) {
  const before = captured().length;
  const asked = await api('/v1/token-challenges', { method: 'POST', body: registration(deviceToken) });
  assert.equal(asked.status, 202);
  const pushes = captured().slice(before);
  assert.equal(pushes.length, 1);
  assert.equal(pushes[0].deviceToken, deviceToken);
  return pushes[0].notification.payload.conduit_token_challenge;
}

async function registerProven(deviceToken, extra = {}) {
  const proof = await challenge(deviceToken);
  return api('/v1/installations', { method: 'POST', body: registration(deviceToken, { token_proof: proof, ...extra }) });
}

before(async () => {
  // A phone registered before token proof, with a PushKit token.
  const fixture = new RelayStore(dataPath);
  const created = fixture.createInstallation({ bundleId: 'com.milim.relay', deviceToken: VICTIM_TOKEN, voipToken: VICTIM_VOIP, environment: 'production' });
  victim = { id: created.installation.id, credential: `${created.installation.id}.${created.deviceSecret}` };
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
      APNS_CAPTURE_PATH: capturePath,
      RELAY_TOKEN_PROOF: '',
      RELAY_TEST_SEAMS: '1',
      TRUST_PROXY: '1',
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

test('the relay says it checks token proof', async () => {
  const meta = await api('/v1/meta', { credential: victim.credential });
  assert.equal(meta.status, 200);
  assert.ok(meta.json.capabilities.includes('token-proof'));
});

test('registering without a proof is refused by default, so a leaked token gains nothing', async () => {
  const leaked = await api('/v1/installations', { method: 'POST', body: registration(VICTIM_TOKEN) });
  assert.equal(leaked.status, 403);
  assert.equal(leaked.json.error, 'token_proof_required');
});

test('the challenge is a silent push carrying a one-time nonce', async () => {
  const token = '3'.repeat(64);
  const before = captured().length;
  await challenge(token);
  const push = captured()[before].notification;
  assert.equal(push.pushType, 'background');
  assert.equal(push.priority, 5);
  assert.equal(push.topic, 'com.milim.relay');
  assert.deepEqual(Object.keys(push.payload).sort(), ['aps', 'conduit_token_challenge']);
  assert.deepEqual(push.payload.aps, { 'content-available': 1 });

  const wrong = await api('/v1/installations', { method: 'POST', body: registration(token, { token_proof: 'A'.repeat(43) }) });
  assert.equal(wrong.status, 403);
  assert.equal(wrong.json.error, 'token_proof_invalid');
});

test('a proven phone registers once per nonce', async () => {
  const token = '4'.repeat(64);
  const proof = await challenge(token);
  const registered = await api('/v1/installations', { method: 'POST', body: registration(token, { token_proof: proof }) });
  assert.equal(registered.status, 201);
  assert.ok(registered.json.credential);
  const replayed = await api('/v1/installations', { method: 'POST', body: registration(token, { token_proof: proof }) });
  assert.equal(replayed.status, 403);
  assert.equal(replayed.json.error, 'token_proof_invalid');
});

test('a nonce proves only the token it was pushed to', async () => {
  const proof = await challenge('5'.repeat(64));
  const elsewhere = await api('/v1/installations', { method: 'POST', body: registration(VICTIM_TOKEN, { token_proof: proof }) });
  assert.equal(elsewhere.status, 403);
  assert.equal(elsewhere.json.error, 'token_proof_invalid');
});

test('an installation moves to a new device token only with its proof; keeping its token needs none', async () => {
  const own = '6'.repeat(64);
  const registered = await registerProven(own);
  const credential = registered.json.credential;
  const id = registered.json.installation.id;

  const same = await api(`/v1/installations/${id}`, { method: 'PUT', credential, body: { device_token: own, preferences: { show_previews: true } } });
  assert.equal(same.status, 200);

  const hijack = await api(`/v1/installations/${id}`, { method: 'PUT', credential, body: { device_token: VICTIM_TOKEN } });
  assert.equal(hijack.status, 403);
  assert.equal(hijack.json.error, 'token_proof_required');

  const next = '7'.repeat(64);
  const proof = await challenge(next);
  const moved = await api(`/v1/installations/${id}`, { method: 'PUT', credential, body: { device_token: next, token_proof: proof } });
  assert.equal(moved.status, 200);
});

test('installations from before token proof keep working', async () => {
  const update = await api(`/v1/installations/${victim.id}`, { method: 'PUT', credential: victim.credential, body: { device_token: VICTIM_TOKEN, preferences: { show_previews: false } } });
  assert.equal(update.status, 200);
  assert.equal(update.json.installation.voip, true);
});

test("another phone's PushKit token is left out, on registering and on updating", async () => {
  const registered = await registerProven('8'.repeat(64), { voip_token: VICTIM_VOIP });
  assert.equal(registered.status, 201);
  assert.equal(registered.json.installation.voip, false);

  const { id } = registered.json.installation;
  const update = await api(`/v1/installations/${id}`, { method: 'PUT', credential: registered.json.credential, body: { voip_token: VICTIM_VOIP } });
  assert.equal(update.status, 200);
  assert.equal(update.json.installation.voip, false);

  // An update naming it keeps the PushKit token the phone already had.
  const own = await registerProven('d'.repeat(64), { voip_token: 'e'.repeat(64) });
  const kept = await api(`/v1/installations/${own.json.installation.id}`, { method: 'PUT', credential: own.json.credential, body: { voip_token: VICTIM_VOIP } });
  assert.equal(kept.json.installation.voip, true);

  // The phone that holds it still rings.
  const victimMeta = await api(`/v1/installations/${victim.id}`, { method: 'PUT', credential: victim.credential, body: {} });
  assert.equal(victimMeta.json.installation.voip, true);
});

test('a proven registration retires the same phone\'s earlier installation and takes back its PushKit token', async () => {
  const token = '9'.repeat(64);
  const voip = 'a'.repeat(64);
  const first = await registerProven(token, { voip_token: voip });
  assert.equal(first.json.installation.voip, true);

  // The app lost its credential (say, a restore) and registers again.
  const second = await registerProven(token, { voip_token: voip });
  assert.equal(second.status, 201);
  assert.equal(second.json.installation.voip, true);

  const stale = await api('/v1/meta', { credential: first.json.credential });
  assert.equal(stale.status, 401);
});

test('challenges for one token are rate limited', async () => {
  const token = 'b'.repeat(64);
  for (let index = 0; index < 5; index += 1) {
    assert.equal((await api('/v1/token-challenges', { method: 'POST', body: registration(token) })).status, 202);
  }
  const flooded = await api('/v1/token-challenges', { method: 'POST', body: registration(token) });
  assert.equal(flooded.status, 429);
});

test('challenges check the app and token shape before pushing', async () => {
  const before = captured().length;
  assert.equal((await api('/v1/token-challenges', { method: 'POST', body: { ...registration('c'.repeat(64)), bundle_id: 'com.example.other' } })).status, 400);
  assert.equal((await api('/v1/token-challenges', { method: 'POST', body: registration('not-a-token') })).status, 400);
  assert.equal(captured().length, before);
});

test("moving to a proven token takes back the PushKit token the phone's stale installation held", async () => {
  // The phone registered once with token T and PushKit token V, then (say,
  // after a restore) registered again with another token, and now moves
  // that installation back to T.
  const token = 'f'.repeat(64);
  const voip = '0'.repeat(63) + '1';
  const stale = await registerProven(token, { voip_token: voip });
  assert.equal(stale.json.installation.voip, true);
  const current = await registerProven('0'.repeat(63) + '2');
  const proof = await challenge(token);
  const moved = await api(`/v1/installations/${current.json.installation.id}`, {
    method: 'PUT', credential: current.json.credential, body: { device_token: token, token_proof: proof, voip_token: voip },
  });
  assert.equal(moved.status, 200);
  assert.equal(moved.json.installation.voip, true);
  assert.equal((await api('/v1/meta', { credential: stale.json.credential })).status, 401, 'the stale one is retired');
});

test('a move with a malformed PushKit token is refused without spending its proof', async () => {
  const registered = await registerProven('c'.repeat(63) + 'd');
  const { id } = registered.json.installation;
  const next = 'c'.repeat(63) + 'e';
  const proof = await challenge(next);
  const bad = await api(`/v1/installations/${id}`, { method: 'PUT', credential: registered.json.credential, body: { device_token: next, token_proof: proof, voip_token: 'nope' } });
  assert.equal(bad.status, 400);
  const moved = await api(`/v1/installations/${id}`, { method: 'PUT', credential: registered.json.credential, body: { device_token: next, token_proof: proof } });
  assert.equal(moved.status, 200);
});

// A Watch moves to a new iPhone with its PushKit token. The old phone's
// installation still holds it, so the new one can't just take it (#146);
// it follows the pairing that the host moves to the new phone.
async function pair(installation) {
  const minted = await api(`/v1/installations/${installation.id}/pairings`, { method: 'POST', credential: installation.credential, body: {} });
  assert.equal(minted.status, 201);
  const claimed = await api('/v1/pairings/claim', { method: 'POST', body: { pairing_code: minted.json.pairing_code, gateway_name: 'Hermes' } });
  assert.equal(claimed.status, 200);
  return claimed.json.credential;
}

async function phone(deviceToken, extra) {
  const registered = await registerProven(deviceToken, extra);
  assert.equal(registered.status, 201);
  return { id: registered.json.installation.id, credential: registered.json.credential, installation: registered.json.installation };
}

const current = async (installation) => (await api(`/v1/installations/${installation.id}`, { method: 'PUT', credential: installation.credential, body: {} })).json.installation;
const revoke = (gateway, query = '') => api(`/v1/gateways/current${query}`, { method: 'DELETE', credential: gateway });

test("a re-pair moves the Watch's ring from the old phone to the new one that asked for it", async () => {
  const watch = '5a'.repeat(32);
  const old = await phone('5b'.repeat(32), { watch_voip_token: watch });
  assert.equal(old.installation.watch_voip, true);
  const gateway = await pair(old);
  await pair(old); // another profile still paired with the old phone

  const moved = await phone('5c'.repeat(32), { watch_voip_token: watch });
  assert.equal(moved.installation.watch_voip, false, 'still the old phone\'s');
  assert.equal(moved.installation.watch_voip_elsewhere, true);
  await pair(moved);

  assert.equal((await revoke(gateway, `?replaced_by=${moved.id}`)).status, 204);
  const now = await current(moved);
  assert.equal(now.watch_voip, true);
  assert.equal(now.watch_voip_elsewhere, undefined);
  assert.equal((await current(old)).watch_voip, false);
});

test('a re-pair to a phone that never asked for the Watch leaves it where it is', async () => {
  const watch = '6a'.repeat(32);
  const old = await phone('6b'.repeat(32), { watch_voip_token: watch });
  const gateway = await pair(old);
  await pair(old);
  const other = await phone('6c'.repeat(32));
  await pair(other);
  assert.equal((await revoke(gateway, `?replaced_by=${other.id}`)).status, 204);
  assert.equal((await current(other)).watch_voip, false);
  assert.equal((await current(old)).watch_voip, true, 'the old phone has another pairing and keeps its Watch');
});

test("an old phone the pairing moved away from lets the Watch go, and the phone that wants it takes it next", async () => {
  const watch = '7a'.repeat(32);
  const old = await phone('7b'.repeat(32), { watch_voip_token: watch });
  const gateway = await pair(old);
  // The new phone hadn't heard from the Watch yet when it was paired.
  const moved = await phone('7c'.repeat(32));
  await pair(moved);
  assert.equal((await revoke(gateway, `?replaced_by=${moved.id}`)).status, 204);
  assert.equal((await current(old)).watch_voip, false);
  assert.equal((await current(moved)).watch_voip, false, 'it never asked');
  const update = await api(`/v1/installations/${moved.id}`, { method: 'PUT', credential: moved.credential, body: { watch_voip_token: watch } });
  assert.equal(update.json.installation.watch_voip, true);
});

test('unpairing keeps the Watch, so pairing the same phone again rings it', async () => {
  const watch = '8a'.repeat(32);
  const own = await phone('8b'.repeat(32), { watch_voip_token: watch });
  const gateway = await pair(own);
  assert.equal((await revoke(gateway)).status, 204);
  await pair(own);
  assert.equal((await current(own)).watch_voip, true);
});
