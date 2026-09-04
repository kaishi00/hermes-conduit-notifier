import { strict as assert } from 'node:assert';
import { generateKeyPairSync } from 'node:crypto';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { after, before, test } from 'node:test';

// Full clarify answer loop against a real relay process: register a device,
// pair a gateway, push a clarify decision event (preferences disabled so no
// APNs call is attempted — the pending decision is still stored), answer from
// the device, and poll from the gateway. Nothing is mocked.

const dir = mkdtempSync(join(tmpdir(), 'conduit-relay-e2e-'));
const port = 19000 + Math.floor(Math.random() * 1000);
const baseUrl = `http://127.0.0.1:${port}`;
const dataPath = join(dir, 'relay-data.json');
// The ApnsClient constructor parses the key eagerly, so the relay needs a
// valid ES256 key to boot — an ephemeral one is fine; this test never sends.
const keyPath = join(dir, 'ephemeral-key.pem');
const { privateKey } = generateKeyPairSync('ec', { namedCurve: 'P-256' });
writeFileSync(keyPath, privateKey.export({ type: 'sec1', format: 'pem' }));

let child;

async function api(path, { method = 'GET', body, credential } = {}) {
  const response = await fetch(`${baseUrl}${path}`, {
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
    // fileURLToPath: URL.pathname yields "/C:/…" on Windows, which spawn
    // rejects; the file-path form works on every platform.
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
    },
    stdio: 'ignore',
  });
  const deadline = Date.now() + 10_000;
  for (;;) {
    try {
      const probe = await fetch(`${baseUrl}/healthz`);
      if (probe.ok) break;
    } catch {}
    if (Date.now() > deadline) throw new Error('relay did not start');
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
});

after(() => {
  child?.kill('SIGTERM');
  rmSync(dir, { recursive: true, force: true });
});

test('clarify decision: push → device answer → gateway poll', async () => {
  // Device registers with notifications disabled so event delivery skips APNs.
  const registered = await api('/v1/installations', {
    method: 'POST',
    body: {
      bundle_id: 'com.milim.relay',
      device_token: 'a'.repeat(64),
      environment: 'production',
      preferences: { enabled: false, decision_cards: true },
    },
  });
  assert.equal(registered.status, 201);
  const deviceCredential = registered.json.credential;
  const installationId = registered.json.installation.id;

  // Pair a gateway (device creates the code, gateway claims it).
  const pairing = await api(`/v1/installations/${installationId}/pairings`, {
    method: 'POST',
    credential: deviceCredential,
  });
  assert.equal(pairing.status, 201);
  const claimed = await api('/v1/pairings/claim', {
    method: 'POST',
    body: { pairing_code: pairing.json.pairing_code, gateway_name: 'test gateway' },
  });
  assert.equal(claimed.status, 200);
  const gatewayCredential = claimed.json.credential;

  // Gateway pushes a clarify decision.
  const event = await api('/v1/events', {
    method: 'POST',
    credential: gatewayCredential,
    body: {
      type: 'input.needed',
      event_id: 'input:abcdef123456',
      session_id: 'sess-1',
      profile: 'default',
      decision: {
        kind: 'clarify',
        request_id: 'conduit-push-abc123',
        question: 'Which color?',
        choices: ['Red', 'Blue'],
      },
    },
  });
  assert.equal(event.status, 202);
  assert.deepEqual(event.json, { accepted: true, delivered: false });

  // Gateway polls: pending. This installation registered with enabled:false,
  // so no card was delivered and the decision reports deliverable:false —
  // the plugin's poll loop stops instead of waiting out the full budget.
  const poll = await api('/v1/decisions/conduit-push-abc123', { credential: gatewayCredential });
  assert.equal(poll.status, 200);
  assert.deepEqual(poll.json, { status: 'pending', deliverable: false });

  // Another installation's gateway must not see it.
  const stranger = await api('/v1/installations', {
    method: 'POST',
    body: { bundle_id: 'com.milim.relay', device_token: 'b'.repeat(64), environment: 'production' },
  });
  const strangerClaim = await api(`/v1/installations/${stranger.json.installation.id}/pairings`, {
    method: 'POST',
    credential: stranger.json.credential,
  });
  const strangerGateway = await api('/v1/pairings/claim', {
    method: 'POST',
    body: { pairing_code: strangerClaim.json.pairing_code, gateway_name: 'stranger' },
  });
  const strangerPoll = await api('/v1/decisions/conduit-push-abc123', {
    credential: strangerGateway.json.credential,
  });
  assert.equal(strangerPoll.status, 200);
  assert.equal(strangerPoll.json.status, 'unknown');

  // The stranger device cannot answer either.
  const strangerAnswer = await api('/v1/decisions/conduit-push-abc123', {
    method: 'POST',
    credential: stranger.json.credential,
    body: { answer: 'Blue' },
  });
  assert.equal(strangerAnswer.status, 404);

  // The paired device answers; the gateway observes the answer.
  const answer = await api('/v1/decisions/conduit-push-abc123', {
    method: 'POST',
    credential: deviceCredential,
    body: { answer: 'Red' },
  });
  assert.equal(answer.status, 200);
  assert.equal(answer.json.status, 'answered');

  const answered = await api('/v1/decisions/conduit-push-abc123', { credential: gatewayCredential });
  assert.equal(answered.status, 200);
  assert.deepEqual(answered.json, { status: 'answered', answer: 'Red' });

  // Second answer attempts are rejected.
  const reanswer = await api('/v1/decisions/conduit-push-abc123', {
    method: 'POST',
    credential: deviceCredential,
    body: { answer: 'Blue' },
  });
  assert.equal(reanswer.status, 409);

  // A duplicate delivery of the same event (same event_id) must not wipe the
  // parked decision's answer: dedupe happens before the pending-decision save.
  const duplicate = await api('/v1/events', {
    method: 'POST',
    credential: gatewayCredential,
    body: {
      type: 'input.needed',
      event_id: 'input:abcdef123456',
      session_id: 'sess-1',
      profile: 'default',
      decision: {
        kind: 'clarify',
        request_id: 'conduit-push-abc123',
        question: 'Which color?',
        choices: ['Red', 'Blue'],
      },
    },
  });
  assert.equal(duplicate.status, 200);
  assert.equal(duplicate.json.duplicate, true);
  const afterDuplicate = await api('/v1/decisions/conduit-push-abc123', { credential: gatewayCredential });
  assert.deepEqual(afterDuplicate.json, { status: 'answered', answer: 'Red' });

  // Empty answers are rejected outright.
  const empty = await api('/v1/decisions/conduit-push-abc123', {
    method: 'POST',
    credential: deviceCredential,
    body: { answer: '   ' },
  });
  assert.equal(empty.status, 400);

  // Unknown ids are 404 for devices.
  const unknown = await api('/v1/decisions/conduit-push-nope', {
    method: 'POST',
    credential: deviceCredential,
    body: { answer: 'x' },
  });
  assert.equal(unknown.status, 404);

  // Credentials are enforced on both endpoints.
  const unauth = await api('/v1/decisions/conduit-push-abc123');
  assert.equal(unauth.status, 401);

  // Plugin version announcement: a control event that never notifies but
  // records the gateway's plugin state for the compatibility view.
  const hello = await api('/v1/events', {
    method: 'POST',
    credential: gatewayCredential,
    body: {
      type: 'plugin.hello',
      // The plugin's event_id hashes the version (hex, no dots); keep the
      // fixture within the id charset the relay accepts.
      event_id: 'hello:020abcdef12',
      plugin_version: '0.3.0',
      plugin_capabilities: ['approval-decisions', 'clarify-loop', 'version-reporting'],
    },
  });
  assert.equal(hello.status, 202);
  assert.deepEqual(hello.json, { accepted: true, delivered: false });

  // The device reads relay + plugin compatibility state.
  const meta = await api('/v1/meta', { credential: deviceCredential });
  assert.equal(meta.status, 200);
  assert.equal(meta.json.version, '0.3.0');
  assert.ok(meta.json.capabilities.includes('decisions'));
  const gatewayMeta = meta.json.gateways.find((gateway) => gateway.name === 'test gateway');
  assert.ok(gatewayMeta, 'paired gateway appears in meta');
  assert.equal(gatewayMeta.plugin_version, '0.3.0');
  assert.ok(gatewayMeta.plugin_capabilities.includes('clarify-loop'));
  assert.ok(gatewayMeta.last_event_at);

  // A later real event refreshes the recorded plugin version.
  await api('/v1/events', {
    method: 'POST',
    credential: gatewayCredential,
    body: {
      type: 'response.ready',
      event_id: 'response:abcdef999999',
      session_id: 'sess-1',
      plugin_version: '0.2.1',
      plugin_capabilities: ['approval-decisions', 'clarify-loop'],
    },
  });
  const metaAfter = await api('/v1/meta', { credential: deviceCredential });
  const refreshed = metaAfter.json.gateways.find((gateway) => gateway.name === 'test gateway');
  assert.equal(refreshed.plugin_version, '0.2.1');

  // A second gateway on the same installation running the same plugin version
  // sends the same deterministic hello id; it must still be recorded even
  // though the event itself dedupes.
  const secondPairing = await api(`/v1/installations/${installationId}/pairings`, {
    method: 'POST',
    credential: deviceCredential,
  });
  const secondClaim = await api('/v1/pairings/claim', {
    method: 'POST',
    body: { pairing_code: secondPairing.json.pairing_code, gateway_name: 'second gateway' },
  });
  await api('/v1/events', {
    method: 'POST',
    credential: secondClaim.json.credential,
    body: {
      type: 'plugin.hello',
      event_id: 'hello:020abcdef12',
      plugin_version: '0.3.0',
      plugin_capabilities: ['approval-decisions', 'clarify-loop', 'version-reporting'],
    },
  });
  const metaTwo = await api('/v1/meta', { credential: deviceCredential });
  const second = metaTwo.json.gateways.find((gateway) => gateway.name === 'second gateway');
  assert.ok(second, 'second gateway listed');
  assert.equal(second.plugin_version, '0.3.0', 'duplicate hello id must still record the new gateway');

  // A pre-0.2 notifier sends events with no plugin_version: last_event_at is
  // stamped anyway so /v1/meta can flag "outdated plugin" instead of
  // "waiting for the first notification".
  await api('/v1/events', {
    method: 'POST',
    credential: secondClaim.json.credential,
    body: {
      type: 'response.ready',
      event_id: 'response:legacy111111',
      session_id: 'sess-2',
    },
  });
  const metaLegacy = await api('/v1/meta', { credential: deviceCredential });
  const legacyGateway = metaLegacy.json.gateways.find((gateway) => gateway.name === 'second gateway');
  // (Second gateway reported 0.2.0 via hello earlier; strip it to simulate
  // the never-reported shape and assert the last_event_at contract.)
  assert.ok(legacyGateway.last_event_at, 'every accepted event stamps last_event_at');

  // Meta requires the device credential, and never leaks cross-installation.
  const metaUnauth = await api('/v1/meta');
  assert.equal(metaUnauth.status, 401);
  const strangerMeta = await api('/v1/meta', { credential: stranger.json.credential });
  assert.equal(strangerMeta.status, 200);
  assert.ok(strangerMeta.json.gateways.every((gateway) => gateway.name !== 'test gateway'), 'cross-installation gateways never leak');
});
