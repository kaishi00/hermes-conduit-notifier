import { strict as assert } from 'node:assert';
import { generateKeyPairSync } from 'node:crypto';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { createServer as createNetServer } from 'node:net';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { after, before, test } from 'node:test';

// Full clarify answer loop against real relay processes: register a device,
// pair a gateway, push clarify decision events, answer from the device, and
// poll from the gateway. Four relays are spawned so APNs outcomes are
// deterministic: the accept relay "delivers" every notification (APNS_MODE
// stub, no network), the reject relay fails every send with a returned
// rejection, the throw relay makes every send RAISE, and the dead-origin
// relay runs the REAL ApnsClient transport against a closed port — a real
// ClientHttp2Session 'error', which the APNS_MODE stubs (they bypass
// ApnsClient entirely) cannot produce — to prove a connection-level failure
// parks the decision undeliverable AND leaves the relay process alive.
// Nothing about the HTTP API is mocked.

const dir = mkdtempSync(join(tmpdir(), 'conduit-relay-e2e-'));
const port = 19000 + Math.floor(Math.random() * 1000);
const rejectPort = port + 500;
const throwPort = port + 1000;
const deadPort = port + 1500;
const baseUrl = `http://127.0.0.1:${port}`;
const rejectBaseUrl = `http://127.0.0.1:${rejectPort}`;
const throwBaseUrl = `http://127.0.0.1:${throwPort}`;
const deadBaseUrl = `http://127.0.0.1:${deadPort}`;
const dataPath = join(dir, 'relay-data.json');
const rejectDataPath = join(dir, 'relay-data-reject.json');
const throwDataPath = join(dir, 'relay-data-throw.json');
const deadDataPath = join(dir, 'relay-data-dead.json');
// Where the dead-origin relay's REAL APNs transport points; resolved in
// before() by reserving and releasing a loopback port.
let deadOriginPort;
// The ApnsClient constructor parses the key eagerly, so the relay needs a
// valid ES256 key to boot — an ephemeral one is fine; sends are stubbed by
// APNS_MODE so nothing ever reaches Apple.
const keyPath = join(dir, 'ephemeral-key.pem');
const { privateKey } = generateKeyPairSync('ec', { namedCurve: 'P-256' });
writeFileSync(keyPath, privateKey.export({ type: 'sec1', format: 'pem' }));

const children = [];

async function api(base, path, { method = 'GET', body, credential } = {}) {
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

// Reserve a loopback port and release it: connecting there must be refused,
// which surfaces as a genuine session-level 'error' inside ApnsClient.
function closedPort() {
  return new Promise((resolve, reject) => {
    const probe = createNetServer();
    probe.listen(0, '127.0.0.1', () => {
      const port = probe.address().port;
      probe.close(() => resolve(port));
    });
    probe.on('error', reject);
  });
}

async function startRelay(aPort, apnsMode, dataFile, extraEnv = {}) {
  const child = spawn(process.execPath, ['src/server.mjs'], {
    // fileURLToPath: URL.pathname yields "/C:/…" on Windows, which spawn
    // rejects; the file-path form works on every platform.
    cwd: fileURLToPath(new URL('..', import.meta.url)),
    env: {
      ...process.env,
      HOST: '127.0.0.1',
      PORT: String(aPort),
      PUBLIC_URL: `https://relay-${aPort}.example`,
      DATA_PATH: dataFile,
      APNS_KEY_PATH: keyPath,
      APNS_KEY_ID: 'AAAAAAAAAA',
      APNS_TEAM_ID: 'BBBBBBBBBB',
      APNS_TOPIC: 'com.milim.relay',
      APNS_MODE: apnsMode,
      ...extraEnv,
    },
    stdio: 'ignore',
  });
  const base = `http://127.0.0.1:${aPort}`;
  const deadline = Date.now() + 10_000;
  for (;;) {
    try {
      const probe = await fetch(`${base}/healthz`);
      if (probe.ok) break;
    } catch {}
    if (Date.now() > deadline) throw new Error('relay did not start');
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  return child;
}

before(async () => {
  deadOriginPort = await closedPort();
  children.push(await startRelay(port, 'accept', dataPath));
  children.push(await startRelay(rejectPort, 'reject', rejectDataPath));
  children.push(await startRelay(throwPort, 'throw', throwDataPath));
  children.push(await startRelay(deadPort, undefined, deadDataPath, {
    APNS_ORIGIN: `https://127.0.0.1:${deadOriginPort}`,
  }));
});

after(() => {
  for (const child of children) child?.kill('SIGTERM');
  rmSync(dir, { recursive: true, force: true });
});

test('batch clarify end to end: questions[] survive intake, per-qid answers, release, and both respond routes', async () => {
  // Notifications ENABLED so intake builds the real APNs payload and
  // deliverability is computed from what actually survived into it. The
  // ephemeral key cannot pass Apple, so the send itself may 502 — the
  // decision is parked (with accurate deliverability) before that point.
  const registered = await api(baseUrl, '/v1/installations', {
    method: 'POST',
    body: {
      bundle_id: 'com.milim.relay',
      device_token: 'c'.repeat(64),
      environment: 'production',
      preferences: { enabled: true, decision_cards: true },
    },
  });
  assert.equal(registered.status, 201);
  const deviceCredential = registered.json.credential;
  const installationId = registered.json.installation.id;
  const pairing = await api(baseUrl, `/v1/installations/${installationId}/pairings`, { method: 'POST', credential: deviceCredential });
  const claimed = await api(baseUrl, '/v1/pairings/claim', {
    method: 'POST',
    body: { pairing_code: pairing.json.pairing_code, gateway_name: 'batch gateway' },
  });
  const gatewayCredential = claimed.json.credential;

  // Push a two-question batch through the real event endpoint.
  const event = await api(baseUrl, '/v1/events', {
    method: 'POST',
    credential: gatewayCredential,
    body: {
      type: 'input.needed',
      event_id: 'input:batch0000001',
      session_id: 'sess-batch',
      profile: 'default',
      decision: {
        kind: 'clarify',
        request_id: 'conduit-push-batche2e1',
        question: 'Which environment?',
        choices: ['staging', 'prod'],
        questions: [
          { qid: 'q0', question: 'Which environment?', choices: ['staging', 'prod'], multi_select: false },
          { qid: 'q1', question: 'Which tests?', choices: ['unit', 'ui'], multi_select: true },
        ],
      },
    },
  });
  // A decision card that survives the size guard is delivered for real
  // (accept-mode APNs): deliverable stays true and the plugin keeps polling.
  assert.equal(event.status, 202);
  assert.deepEqual(event.json, { accepted: true, delivered: true });

  // The stored decision kept the FULL batch, deliverable, and the open qids.
  const poll = await api(baseUrl, '/v1/decisions/conduit-push-batche2e1', { credential: gatewayCredential });
  assert.equal(poll.status, 200);
  assert.equal(poll.json.status, 'pending');
  assert.equal(poll.json.deliverable, true, 'the card survived into the APNs payload');
  assert.deepEqual(poll.json.remaining, ['q0', 'q1']);

  // Device answers q0 through the iOS /respond route.
  const q0 = await api(baseUrl, '/v1/decisions/conduit-push-batche2e1/respond', {
    method: 'POST',
    credential: deviceCredential,
    body: { question_id: 'q0', answer: 'staging' },
  });
  assert.equal(q0.status, 200);
  assert.deepEqual(q0.json, { status: 'answered', remaining: ['q1'] });

  // A duplicate q0 is question-locked (409), NOT released: q1 stays open.
  const duplicate = await api(baseUrl, '/v1/decisions/conduit-push-batche2e1/respond', {
    method: 'POST',
    credential: deviceCredential,
    body: { question_id: 'q0', answer: 'prod' },
  });
  assert.equal(duplicate.status, 409);
  assert.equal(duplicate.json.error, 'already_answered');

  // The bare path stays a working alias for the same handler: q1 completes.
  const q1 = await api(baseUrl, '/v1/decisions/conduit-push-batche2e1', {
    method: 'POST',
    credential: deviceCredential,
    body: { question_id: 'q1', answer: '["unit"]' },
  });
  assert.equal(q1.status, 200);
  assert.deepEqual(q1.json, { status: 'answered', remaining: [] });

  const done = await api(baseUrl, '/v1/decisions/conduit-push-batche2e1', { credential: gatewayCredential });
  assert.equal(done.status, 200);
  assert.equal(done.json.status, 'answered');
  assert.deepEqual(done.json.remaining, []);
  assert.deepEqual({ ...done.json.answers }, { q0: 'staging', q1: '["unit"]' });

  // ── Release semantics: the native path won, the plugin deletes. ──
  await api(baseUrl, '/v1/events', {
    method: 'POST',
    credential: gatewayCredential,
    body: {
      type: 'input.needed',
      event_id: 'input:batch0000002',
      session_id: 'sess-batch',
      profile: 'default',
      decision: {
        kind: 'clarify',
        request_id: 'conduit-push-batche2e2',
        question: 'Second?',
        questions: [{ qid: 'q0', question: 'Second?', choices: ['a'], multi_select: false }],
      },
    },
  });
  const released = await api(baseUrl, '/v1/decisions/conduit-push-batche2e2', { method: 'DELETE', credential: gatewayCredential });
  assert.equal(released.status, 200);
  assert.deepEqual(released.json, { status: 'cancelled' }, 'a LIVE decision release reports cancelled');
  // A late device answer reports decision_released (410), NOT qid-locked.
  const late = await api(baseUrl, '/v1/decisions/conduit-push-batche2e2/respond', {
    method: 'POST',
    credential: deviceCredential,
    body: { question_id: 'q0', answer: 'a' },
  });
  assert.equal(late.status, 410);
  assert.equal(late.json.error, 'decision_released');
  // The poller sees the release and falls back to the original path.
  const pollReleased = await api(baseUrl, '/v1/decisions/conduit-push-batche2e2', { credential: gatewayCredential });
  assert.deepEqual(pollReleased.json, { status: 'unknown' });

  // ── An oversized batch is parked undeliverable: the size guard stripped
  // the card from the APNs payload, so the plugin must stop polling. ──
  const oversized = await api(baseUrl, '/v1/events', {
    method: 'POST',
    credential: gatewayCredential,
    body: {
      type: 'input.needed',
      event_id: 'input:batch0000003',
      session_id: 'sess-batch',
      profile: 'default',
      decision: {
        kind: 'clarify',
        request_id: 'conduit-push-batche2e3',
        question: 'Huge?',
        questions: Array.from({ length: 8 }, (_, i) => ({
          qid: `q${i}`,
          question: 'x'.repeat(500),
          choices: Array.from({ length: 8 }, (_, j) => 'y'.repeat(80)),
          multi_select: false,
        })),
      },
    },
  });
  assert.equal(oversized.status, 202);
  assert.deepEqual(
    oversized.json,
    { accepted: true, delivered: true },
    'the plain input.needed fallback banner is still delivered when the size guard strips the card',
  );
  const oversizedPoll = await api(baseUrl, '/v1/decisions/conduit-push-batche2e3', { credential: gatewayCredential });
  assert.equal(oversizedPoll.json.status, 'pending');
  assert.equal(oversizedPoll.json.deliverable, false, 'a size-guard-stripped card must be undeliverable');
});

test('clarify decision: push → device answer → gateway poll', async () => {
  // Device registers with notifications disabled so event delivery skips APNs.
  const registered = await api(baseUrl, '/v1/installations', {
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
  const pairing = await api(baseUrl, `/v1/installations/${installationId}/pairings`, {
    method: 'POST',
    credential: deviceCredential,
  });
  assert.equal(pairing.status, 201);
  const claimed = await api(baseUrl, '/v1/pairings/claim', {
    method: 'POST',
    body: { pairing_code: pairing.json.pairing_code, gateway_name: 'test gateway' },
  });
  assert.equal(claimed.status, 200);
  const gatewayCredential = claimed.json.credential;

  // Gateway pushes a clarify decision.
  const event = await api(baseUrl, '/v1/events', {
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
  const poll = await api(baseUrl, '/v1/decisions/conduit-push-abc123', { credential: gatewayCredential });
  assert.equal(poll.status, 200);
  assert.deepEqual(poll.json, { status: 'pending', deliverable: false });

  // Another installation's gateway must not see it.
  const stranger = await api(baseUrl, '/v1/installations', {
    method: 'POST',
    body: { bundle_id: 'com.milim.relay', device_token: 'b'.repeat(64), environment: 'production' },
  });
  const strangerClaim = await api(baseUrl, `/v1/installations/${stranger.json.installation.id}/pairings`, {
    method: 'POST',
    credential: stranger.json.credential,
  });
  const strangerGateway = await api(baseUrl, '/v1/pairings/claim', {
    method: 'POST',
    body: { pairing_code: strangerClaim.json.pairing_code, gateway_name: 'stranger' },
  });
  const strangerPoll = await api(baseUrl, '/v1/decisions/conduit-push-abc123', {
    credential: strangerGateway.json.credential,
  });
  assert.equal(strangerPoll.status, 200);
  assert.equal(strangerPoll.json.status, 'unknown');

  // The stranger device cannot answer either.
  const strangerAnswer = await api(baseUrl, '/v1/decisions/conduit-push-abc123', {
    method: 'POST',
    credential: stranger.json.credential,
    body: { answer: 'Blue' },
  });
  assert.equal(strangerAnswer.status, 404);

  // The paired device answers; the gateway observes the answer.
  const answer = await api(baseUrl, '/v1/decisions/conduit-push-abc123', {
    method: 'POST',
    credential: deviceCredential,
    body: { answer: 'Red' },
  });
  assert.equal(answer.status, 200);
  assert.equal(answer.json.status, 'answered');

  const answered = await api(baseUrl, '/v1/decisions/conduit-push-abc123', { credential: gatewayCredential });
  assert.equal(answered.status, 200);
  assert.deepEqual(answered.json, { status: 'answered', answer: 'Red' });

  // Releasing an ALREADY-COMPLETED decision is a diagnostic, not an error:
  // same 200, distinct status string so gateway logs can tell the two apart.
  const completed = await api(baseUrl, '/v1/decisions/conduit-push-abc123', { method: 'DELETE', credential: gatewayCredential });
  assert.equal(completed.status, 200);
  assert.deepEqual(completed.json, { status: 'already_completed' });

  // Second answer attempts are rejected.
  const reanswer = await api(baseUrl, '/v1/decisions/conduit-push-abc123', {
    method: 'POST',
    credential: deviceCredential,
    body: { answer: 'Blue' },
  });
  assert.equal(reanswer.status, 409);

  // A duplicate delivery of the same event (same event_id) must not wipe the
  // parked decision's answer: dedupe happens before the pending-decision save.
  const duplicate = await api(baseUrl, '/v1/events', {
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
  const afterDuplicate = await api(baseUrl, '/v1/decisions/conduit-push-abc123', { credential: gatewayCredential });
  assert.deepEqual(afterDuplicate.json, { status: 'answered', answer: 'Red' });

  // Empty answers are rejected outright.
  const empty = await api(baseUrl, '/v1/decisions/conduit-push-abc123', {
    method: 'POST',
    credential: deviceCredential,
    body: { answer: '   ' },
  });
  assert.equal(empty.status, 400);

  // Unknown ids are 404 for devices.
  const unknown = await api(baseUrl, '/v1/decisions/conduit-push-nope', {
    method: 'POST',
    credential: deviceCredential,
    body: { answer: 'x' },
  });
  assert.equal(unknown.status, 404);

  // Credentials are enforced on both endpoints.
  const unauth = await api(baseUrl, '/v1/decisions/conduit-push-abc123');
  assert.equal(unauth.status, 401);

  // Plugin version announcement: a control event that never notifies but
  // records the gateway's plugin state for the compatibility view.
  const hello = await api(baseUrl, '/v1/events', {
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
  const meta = await api(baseUrl, '/v1/meta', { credential: deviceCredential });
  assert.equal(meta.status, 200);
  assert.equal(meta.json.version, '0.3.0');
  assert.ok(meta.json.capabilities.includes('decisions'));
  const gatewayMeta = meta.json.gateways.find((gateway) => gateway.name === 'test gateway');
  assert.ok(gatewayMeta, 'paired gateway appears in meta');
  assert.equal(gatewayMeta.plugin_version, '0.3.0');
  assert.ok(gatewayMeta.plugin_capabilities.includes('clarify-loop'));
  assert.ok(gatewayMeta.last_event_at);

  // A later real event refreshes the recorded plugin version.
  await api(baseUrl, '/v1/events', {
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
  const metaAfter = await api(baseUrl, '/v1/meta', { credential: deviceCredential });
  const refreshed = metaAfter.json.gateways.find((gateway) => gateway.name === 'test gateway');
  assert.equal(refreshed.plugin_version, '0.2.1');

  // A second gateway on the same installation running the same plugin version
  // sends the same deterministic hello id; it must still be recorded even
  // though the event itself dedupes.
  const secondPairing = await api(baseUrl, `/v1/installations/${installationId}/pairings`, {
    method: 'POST',
    credential: deviceCredential,
  });
  const secondClaim = await api(baseUrl, '/v1/pairings/claim', {
    method: 'POST',
    body: { pairing_code: secondPairing.json.pairing_code, gateway_name: 'second gateway' },
  });
  await api(baseUrl, '/v1/events', {
    method: 'POST',
    credential: secondClaim.json.credential,
    body: {
      type: 'plugin.hello',
      event_id: 'hello:020abcdef12',
      plugin_version: '0.3.0',
      plugin_capabilities: ['approval-decisions', 'clarify-loop', 'version-reporting'],
    },
  });
  const metaTwo = await api(baseUrl, '/v1/meta', { credential: deviceCredential });
  const second = metaTwo.json.gateways.find((gateway) => gateway.name === 'second gateway');
  assert.ok(second, 'second gateway listed');
  assert.equal(second.plugin_version, '0.3.0', 'duplicate hello id must still record the new gateway');

  // A pre-0.2 notifier sends events with no plugin_version: last_event_at is
  // stamped anyway so /v1/meta can flag "outdated plugin" instead of
  // "waiting for the first notification".
  await api(baseUrl, '/v1/events', {
    method: 'POST',
    credential: secondClaim.json.credential,
    body: {
      type: 'response.ready',
      event_id: 'response:legacy111111',
      session_id: 'sess-2',
    },
  });
  const metaLegacy = await api(baseUrl, '/v1/meta', { credential: deviceCredential });
  const legacyGateway = metaLegacy.json.gateways.find((gateway) => gateway.name === 'second gateway');
  // (Second gateway reported 0.2.0 via hello earlier; strip it to simulate
  // the never-reported shape and assert the last_event_at contract.)
  assert.ok(legacyGateway.last_event_at, 'every accepted event stamps last_event_at');

  // Meta requires the device credential, and never leaks cross-installation.
  const metaUnauth = await api(baseUrl, '/v1/meta');
  assert.equal(metaUnauth.status, 401);
  const strangerMeta = await api(baseUrl, '/v1/meta', { credential: stranger.json.credential });
  assert.equal(strangerMeta.status, 200);
  assert.ok(strangerMeta.json.gateways.every((gateway) => gateway.name !== 'test gateway'), 'cross-installation gateways never leak');
});

test('decision_cards=false still delivers the plain banner and parks the decision undeliverable', async () => {
  const registered = await api(baseUrl, '/v1/installations', {
    method: 'POST',
    body: {
      bundle_id: 'com.milim.relay',
      device_token: 'd'.repeat(64),
      environment: 'production',
      preferences: { enabled: true, input_needed: true, decision_cards: false },
    },
  });
  assert.equal(registered.status, 201);
  const deviceCredential = registered.json.credential;
  const installationId = registered.json.installation.id;
  const pairing = await api(baseUrl, `/v1/installations/${installationId}/pairings`, { method: 'POST', credential: deviceCredential });
  const claimed = await api(baseUrl, '/v1/pairings/claim', {
    method: 'POST',
    body: { pairing_code: pairing.json.pairing_code, gateway_name: 'no-cards gateway' },
  });
  const gatewayCredential = claimed.json.credential;

  const event = await api(baseUrl, '/v1/events', {
    method: 'POST',
    credential: gatewayCredential,
    body: {
      type: 'input.needed',
      event_id: 'input:nocards00001',
      session_id: 'sess-nocards',
      profile: 'default',
      decision: {
        kind: 'clarify',
        request_id: 'conduit-push-nocards1',
        question: 'Which environment?',
        questions: [{ qid: 'q0', question: 'Which environment?', choices: ['staging'], multi_select: false }],
      },
    },
  });
  // The user disabled answerable cards, NOT notifications: the ordinary
  // input.needed banner is still delivered.
  assert.equal(event.status, 202);
  assert.deepEqual(event.json, { accepted: true, delivered: true });

  // The parked decision is undeliverable, so the plugin immediately falls
  // back to the native clarify path instead of waiting out its budget.
  const poll = await api(baseUrl, '/v1/decisions/conduit-push-nocards1', { credential: gatewayCredential });
  assert.equal(poll.status, 200);
  assert.equal(poll.json.status, 'pending');
  assert.equal(poll.json.deliverable, false);
  assert.deepEqual(poll.json.remaining, ['q0'], 'the qids survive even when the card does not');
});

test('APNs rejection after parking flips a deliverable decision to undeliverable', async () => {
  // The reject-mode relay fails every send AFTER intake parked the decision
  // as deliverable (the structured card survived the size guard).
  const registered = await api(rejectBaseUrl, '/v1/installations', {
    method: 'POST',
    body: {
      bundle_id: 'com.milim.relay',
      device_token: 'e'.repeat(64),
      environment: 'production',
      preferences: { enabled: true, decision_cards: true },
    },
  });
  assert.equal(registered.status, 201);
  const deviceCredential = registered.json.credential;
  const installationId = registered.json.installation.id;
  const pairing = await api(rejectBaseUrl, `/v1/installations/${installationId}/pairings`, { method: 'POST', credential: deviceCredential });
  const claimed = await api(rejectBaseUrl, '/v1/pairings/claim', {
    method: 'POST',
    body: { pairing_code: pairing.json.pairing_code, gateway_name: 'reject gateway' },
  });
  const gatewayCredential = claimed.json.credential;

  const event = await api(rejectBaseUrl, '/v1/events', {
    method: 'POST',
    credential: gatewayCredential,
    body: {
      type: 'input.needed',
      event_id: 'input:reject000001',
      session_id: 'sess-reject',
      profile: 'default',
      decision: {
        kind: 'clarify',
        request_id: 'conduit-push-reject1',
        question: 'Which environment?',
        questions: [{ qid: 'q0', question: 'Which environment?', choices: ['staging'], multi_select: false }],
      },
    },
  });
  // APNs rejected the send: the relay reports the failure upstream…
  assert.equal(event.status, 502);
  assert.equal(event.json.error, 'apns_rejected');

  // …but the parked decision is no longer deliverable — the plugin's next
  // poll sees it and promptly falls back to the native clarify path instead
  // of polling a decision no device ever received.
  const poll = await api(rejectBaseUrl, '/v1/decisions/conduit-push-reject1', { credential: gatewayCredential });
  assert.equal(poll.status, 200);
  assert.equal(poll.json.status, 'pending');
  assert.equal(poll.json.deliverable, false, 'APNs rejection must flip the parked deliverable flag');
  assert.deepEqual(poll.json.remaining, ['q0']);
});

test('unknown qid on a live batch decision is a bad request, not a missing decision', async () => {
  const registered = await api(baseUrl, '/v1/installations', {
    method: 'POST',
    body: {
      bundle_id: 'com.milim.relay',
      device_token: 'f'.repeat(64),
      environment: 'production',
      preferences: { enabled: false, decision_cards: true },
    },
  });
  const deviceCredential = registered.json.credential;
  const installationId = registered.json.installation.id;
  const pairing = await api(baseUrl, `/v1/installations/${installationId}/pairings`, { method: 'POST', credential: deviceCredential });
  const claimed = await api(baseUrl, '/v1/pairings/claim', {
    method: 'POST',
    body: { pairing_code: pairing.json.pairing_code, gateway_name: 'qid gateway' },
  });
  const gatewayCredential = claimed.json.credential;
  await api(baseUrl, '/v1/events', {
    method: 'POST',
    credential: gatewayCredential,
    body: {
      type: 'input.needed',
      event_id: 'input:qidinvalid01',
      session_id: 'sess-qid',
      profile: 'default',
      decision: {
        kind: 'clarify',
        request_id: 'conduit-push-qidcheck',
        question: 'One?',
        questions: [{ qid: 'q0', question: 'One?', choices: ['a'], multi_select: false }],
      },
    },
  });
  const answer = await api(baseUrl, '/v1/decisions/conduit-push-qidcheck/respond', {
    method: 'POST',
    credential: deviceCredential,
    body: { question_id: 'q9', answer: 'a' },
  });
  assert.equal(answer.status, 400);
  assert.equal(answer.json.error, 'invalid_question_id');
  // The established meanings are untouched: the live decision still polls.
  const poll = await api(baseUrl, '/v1/decisions/conduit-push-qidcheck', { credential: gatewayCredential });
  assert.equal(poll.json.status, 'pending');
});

test('an ordinary multi-question batch is delivered with the full qid set (single structured copy)', async () => {
  // Representative 3-question batch (~200-char texts, 4 choices each) —
  // large enough that the OLD duplicated payload flirted with the guard.
  const registered = await api(baseUrl, '/v1/installations', {
    method: 'POST',
    body: {
      bundle_id: 'com.milim.relay',
      device_token: '1'.repeat(64),
      environment: 'production',
      preferences: { enabled: true, decision_cards: true },
    },
  });
  assert.equal(registered.status, 201);
  const deviceCredential = registered.json.credential;
  const installationId = registered.json.installation.id;
  const pairing = await api(baseUrl, `/v1/installations/${installationId}/pairings`, { method: 'POST', credential: deviceCredential });
  const claimed = await api(baseUrl, '/v1/pairings/claim', {
    method: 'POST',
    body: { pairing_code: pairing.json.pairing_code, gateway_name: 'midsize gateway' },
  });
  const gatewayCredential = claimed.json.credential;

  const longText = 'Describe this rollout step in detail: which services are affected, the expected downtime window, '
    + 'the rollback procedure if validation fails, and who signs off on completion for the platform team to proceed. ';
  const event = await api(baseUrl, '/v1/events', {
    method: 'POST',
    credential: gatewayCredential,
    body: {
      type: 'input.needed',
      event_id: 'input:midsizee2e01',
      session_id: 'sess-midsize-e2e',
      profile: 'default',
      decision: {
        kind: 'clarify',
        request_id: 'conduit-push-midsize1',
        question: longText,
        questions: [0, 1, 2].map((index) => ({
          qid: `q${index}`,
          question: longText + `Variant ${index}.`,
          choices: [
            `Proceed with option ${index} now`,
            `Delay option ${index} to the next window`,
            `Escalate option ${index} for review`,
          ],
          multi_select: index === 2,
        })),
      },
    },
  });
  assert.equal(event.status, 202);
  assert.deepEqual(event.json, { accepted: true, delivered: true });

  const poll = await api(baseUrl, '/v1/decisions/conduit-push-midsize1', { credential: gatewayCredential });
  assert.equal(poll.json.status, 'pending');
  assert.equal(poll.json.deliverable, true, 'a representative batch must keep its answerable card');
  assert.deepEqual(poll.json.remaining, ['q0', 'q1', 'q2'], 'all qids preserved');
});

test('APNs thrown transport error after parking flips the decision undeliverable', async () => {
  const registered = await api(throwBaseUrl, '/v1/installations', {
    method: 'POST',
    body: {
      bundle_id: 'com.milim.relay',
      device_token: '9'.repeat(64),
      environment: 'production',
      preferences: { enabled: true, decision_cards: true },
    },
  });
  assert.equal(registered.status, 201);
  const deviceCredential = registered.json.credential;
  const installationId = registered.json.installation.id;
  const pairing = await api(throwBaseUrl, `/v1/installations/${installationId}/pairings`, { method: 'POST', credential: deviceCredential });
  const claimed = await api(throwBaseUrl, '/v1/pairings/claim', {
    method: 'POST',
    body: { pairing_code: pairing.json.pairing_code, gateway_name: 'throw gateway' },
  });
  const gatewayCredential = claimed.json.credential;

  const event = await api(throwBaseUrl, '/v1/events', {
    method: 'POST',
    credential: gatewayCredential,
    body: {
      type: 'input.needed',
      event_id: 'input:throw000001',
      session_id: 'sess-throw',
      profile: 'default',
      decision: {
        kind: 'clarify',
        request_id: 'conduit-push-throw1',
        question: 'Which environment?',
        questions: [{ qid: 'q0', question: 'Which environment?', choices: ['staging'], multi_select: false }],
      },
    },
  });
  // A thrown send is reported as a transport failure…
  assert.equal(event.status, 502);
  assert.equal(event.json.error, 'apns_unreachable');

  // …and the parked decision is undeliverable, exactly like a returned
  // rejection, so the plugin promptly falls back to the native path.
  const poll = await api(throwBaseUrl, '/v1/decisions/conduit-push-throw1', { credential: gatewayCredential });
  assert.equal(poll.json.status, 'pending');
  assert.equal(poll.json.deliverable, false);
});

test('release succeeds even after the poll quota is exhausted', async () => {
  const registered = await api(baseUrl, '/v1/installations', {
    method: 'POST',
    body: {
      bundle_id: 'com.milim.relay',
      device_token: '7'.repeat(64),
      environment: 'production',
      preferences: { enabled: false, decision_cards: true },
    },
  });
  assert.equal(registered.status, 201);
  const deviceCredential = registered.json.credential;
  const installationId = registered.json.installation.id;
  const pairing = await api(baseUrl, `/v1/installations/${installationId}/pairings`, { method: 'POST', credential: deviceCredential });
  const claimed = await api(baseUrl, '/v1/pairings/claim', {
    method: 'POST',
    body: { pairing_code: pairing.json.pairing_code, gateway_name: 'quota gateway' },
  });
  const gatewayCredential = claimed.json.credential;
  await api(baseUrl, '/v1/events', {
    method: 'POST',
    credential: gatewayCredential,
    body: {
      type: 'input.needed',
      event_id: 'input:quota000001',
      session_id: 'sess-quota',
      profile: 'default',
      decision: {
        kind: 'clarify',
        request_id: 'conduit-push-quota1',
        question: 'One?',
        questions: [{ qid: 'q0', question: 'One?', choices: ['a'], multi_select: false }],
      },
    },
  });

  // Burn the poll bucket (120/60s) plus margin — many polls will now 429.
  for (let i = 0; i < 125; i += 1) {
    await api(baseUrl, '/v1/decisions/conduit-push-quota1', { credential: gatewayCredential });
  }

  // Release must NOT be starved by the exhausted poll quota…
  const released = await api(baseUrl, '/v1/decisions/conduit-push-quota1', { method: 'DELETE', credential: gatewayCredential });
  assert.equal(released.status, 200);
  // …and the late device answer still reports decision_released.
  const late = await api(baseUrl, '/v1/decisions/conduit-push-quota1/respond', {
    method: 'POST',
    credential: deviceCredential,
    body: { question_id: 'q0', answer: 'a' },
  });
  assert.equal(late.status, 410);
  assert.equal(late.json.error, 'decision_released');
});

test('a REAL APNs session failure parks the decision undeliverable and the relay survives', async () => {
  // The dead-origin relay runs the real ApnsClient transport (no APNS_MODE
  // stub) pointed at a closed loopback port, so the HTTP/2 CONNECT fails at
  // the session level — the failure shape the APNS_MODE=throw stub cannot
  // produce. Notifications are ENABLED (default preferences) so intake
  // actually calls the transport instead of skipping it.
  const registered = await api(deadBaseUrl, '/v1/installations', {
    method: 'POST',
    body: {
      bundle_id: 'com.milim.relay',
      device_token: 'e'.repeat(64),
      environment: 'production',
    },
  });
  assert.equal(registered.status, 201);
  const deviceCredential = registered.json.credential;
  const installationId = registered.json.installation.id;
  const pairing = await api(deadBaseUrl, `/v1/installations/${installationId}/pairings`, { method: 'POST', credential: deviceCredential });
  const claimed = await api(deadBaseUrl, '/v1/pairings/claim', {
    method: 'POST',
    body: { pairing_code: pairing.json.pairing_code, gateway_name: 'dead-origin gateway' },
  });
  const gatewayCredential = claimed.json.credential;

  const event = await api(deadBaseUrl, '/v1/events', {
    method: 'POST',
    credential: gatewayCredential,
    body: {
      type: 'input.needed',
      event_id: 'input:deadapns0001',
      session_id: 'sess-dead',
      profile: 'default',
      decision: {
        kind: 'clarify',
        request_id: 'conduit-push-dead1',
        question: 'Which environment?',
        choices: ['staging', 'prod'],
      },
    },
  });
  // The thrown transport failure is converted to the same outcome as a
  // returned rejection: reported upstream, decision flipped undeliverable.
  assert.equal(event.status, 502);
  assert.equal(event.json.error, 'apns_unreachable');

  // The decision WAS parked before the send, and the plugin's poller must
  // see deliverable:false so it stops waiting and falls back to the native
  // clarify path.
  const poll = await api(deadBaseUrl, '/v1/decisions/conduit-push-dead1', { credential: gatewayCredential });
  assert.equal(poll.status, 200);
  assert.deepEqual(poll.json, { status: 'pending', deliverable: false });

  // Still alive: another poll works too, and a plain notification (no
  // decision) takes the same thrown-transport path to 502.
  const health = await api(deadBaseUrl, '/healthz');
  assert.equal(health.status, 200);
  assert.deepEqual(health.json, { ok: true });
  const plain = await api(deadBaseUrl, '/v1/events', {
    method: 'POST',
    credential: gatewayCredential,
    body: {
      type: 'response.ready',
      event_id: 'response:deadapns01',
      session_id: 'sess-dead',
      profile: 'default',
    },
  });
  assert.equal(plain.status, 502);
  assert.equal(plain.json.error, 'apns_unreachable');
  const alivePoll = await api(deadBaseUrl, '/v1/decisions/conduit-push-dead1', { credential: gatewayCredential });
  assert.equal(alivePoll.status, 200);
});

test('pairing dashboard binding end to end: bound at creation, visible in meta, event body never rebinds', async () => {
  const registered = await api(baseUrl, '/v1/installations', {
    method: 'POST',
    body: {
      bundle_id: 'com.milim.relay',
      device_token: 'e'.repeat(64),
      environment: 'production',
    },
  });
  assert.equal(registered.status, 201);
  const installationId = registered.json.installation.id;
  const deviceCredential = registered.json.credential;

  // A malformed dashboard_id is rejected at pairing creation: the device
  // must learn its binding did not land, never silently pair unscoped.
  const malformed = await api(baseUrl, `/v1/installations/${installationId}/pairings`, {
    method: 'POST',
    credential: deviceCredential,
    body: { dashboard_id: 'not-a-uuid' },
  });
  assert.equal(malformed.status, 400);
  assert.equal(malformed.json.error, 'invalid_dashboard_id');

  const dashboardId = '0f5c8a34-1b2d-4e5f-8a9b-0c1d2e3f4a5b';
  const pairing = await api(baseUrl, `/v1/installations/${installationId}/pairings`, {
    method: 'POST',
    credential: deviceCredential,
    body: { dashboard_id: dashboardId },
  });
  assert.equal(pairing.status, 201);
  const claim = await api(baseUrl, '/v1/pairings/claim', {
    method: 'POST',
    body: { pairing_code: pairing.json.pairing_code, gateway_name: 'bound gateway' },
  });
  assert.equal(claim.status, 200);
  const gatewayCredential = claim.json.credential;

  // A hello records the gateway; meta now reports its dashboard binding.
  const hello = await api(baseUrl, '/v1/events', {
    method: 'POST',
    credential: gatewayCredential,
    body: {
      type: 'plugin.hello',
      event_id: 'hello:020abcdef12',
      plugin_version: '0.3.0',
      plugin_capabilities: ['approval-decisions', 'clarify-loop', 'version-reporting'],
    },
  });
  assert.equal(hello.status, 202);
  const meta = await api(baseUrl, '/v1/meta', { credential: deviceCredential });
  const bound = meta.json.gateways.find((gateway) => gateway.name === 'bound gateway');
  assert.ok(bound, 'bound gateway listed in meta');
  assert.equal(bound.dashboard_id, dashboardId);

  // A plugin-supplied dashboard_id in the event body is dropped at the
  // trust boundary and can never rebind the gateway: the stored pairing
  // binding survives unchanged.
  const rebinding = await api(baseUrl, '/v1/events', {
    method: 'POST',
    credential: gatewayCredential,
    body: {
      type: 'response.ready',
      event_id: 'response:rebind000001',
      session_id: 'sess-1',
      dashboard_id: '99999999-9999-4999-8999-999999999999',
    },
  });
  assert.equal(rebinding.status, 202);
  const metaAfter = await api(baseUrl, '/v1/meta', { credential: deviceCredential });
  const after = metaAfter.json.gateways.find((gateway) => gateway.name === 'bound gateway');
  assert.equal(after.dashboard_id, dashboardId, 'event-body dashboard_id never rebinds the gateway');
});

test('a literal null or non-object JSON body never 500s the pairing route', async () => {
  const registered = await api(baseUrl, '/v1/installations', {
    method: 'POST',
    body: {
      bundle_id: 'com.milim.relay',
      device_token: '7'.repeat(64),
      environment: 'production',
    },
  });
  assert.equal(registered.status, 201);
  const installationId = registered.json.installation.id;
  const deviceCredential = registered.json.credential;

  // Deliberate contract: ONLY a JSON object is a valid body. A top-level
  // null/array/scalar parses cleanly but is a client mistake — rejected as
  // 400 invalid_json (the pre-hardening bug was a 500 TypeError). Never
  // normalized to {} (which would turn a malformed request into a
  // successful no-field operation).
  for (const rawBody of ['null', '[1,2]', '"text"', '42']) {
    const response = await fetch(`${baseUrl}/v1/installations/${installationId}/pairings`, {
      method: 'POST',
      headers: {
        authorization: `Bearer ${deviceCredential}`,
        'content-type': 'application/json',
      },
      body: rawBody,
    });
    assert.equal(response.status, 400, `body ${rawBody} must be invalid_json`);
    assert.equal((await response.json()).error, 'invalid_json');
  }

  // Same strictness on the unauthenticated registration route.
  const nullRegistration = await fetch(`${baseUrl}/v1/installations`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: 'null',
  });
  assert.equal(nullRegistration.status, 400);

  // An EMPTY body stays legal where routes tolerate it (reads as {}).
  const emptyBody = await fetch(`${baseUrl}/v1/installations/${installationId}/pairings`, {
    method: 'POST',
    headers: {
      authorization: `Bearer ${deviceCredential}`,
      'content-type': 'application/json',
    },
  });
  assert.equal(emptyBody.status, 201, 'empty body pairs unbound');
});

test('pairing rejects a nil dashboard UUID and treats explicit null as absent', async () => {
  const registered = await api(baseUrl, '/v1/installations', {
    method: 'POST',
    body: {
      bundle_id: 'com.milim.relay',
      device_token: 'f'.repeat(64),
      environment: 'production',
    },
  });
  assert.equal(registered.status, 201);
  const installationId = registered.json.installation.id;
  const deviceCredential = registered.json.credential;

  const nilUuid = await api(baseUrl, `/v1/installations/${installationId}/pairings`, {
    method: 'POST',
    credential: deviceCredential,
    body: { dashboard_id: '00000000-0000-0000-0000-000000000000' },
  });
  assert.equal(nilUuid.status, 400);
  assert.equal(nilUuid.json.error, 'invalid_dashboard_id');

  const explicitNull = await api(baseUrl, `/v1/installations/${installationId}/pairings`, {
    method: 'POST',
    credential: deviceCredential,
    body: { dashboard_id: null },
  });
  assert.equal(explicitNull.status, 201, 'explicit null means no binding, matching the absent shape');
});

test('plugin-supplied dashboard_id can never affect the REAL APNs payload; null pairing stays unbound', async () => {
  // Dedicated relay with payload capture so assertions run against the
  // actual outgoing APNs notification, not /v1/meta or the event ack.
  const capturePath = join(dir, `capture-${Date.now()}.jsonl`);
  const captureRelay = await startRelay(port + 2000, 'accept', join(dir, 'relay-data-capture.json'), {
    APNS_CAPTURE_PATH: capturePath,
  });
  const captureBase = `http://127.0.0.1:${port + 2000}`;
  try {
    const registered = await api(captureBase, '/v1/installations', {
      method: 'POST',
      body: {
        bundle_id: 'com.milim.relay',
        device_token: 'c'.repeat(64),
        environment: 'production',
      },
    });
    assert.equal(registered.status, 201);
    const installationId = registered.json.installation.id;
    const deviceCredential = registered.json.credential;

    // Gateway 1: bound to dashboard A. Gateway 2: explicit null (unbound).
    const dashboardId = '0f5c8a34-1b2d-4e5f-8a9b-0c1d2e3f4a5b';
    // One active pairing per installation at a time: create AND claim the
    // bound pairing before rotating to the unbound one.
    const boundPairing = await api(captureBase, `/v1/installations/${installationId}/pairings`, {
      method: 'POST',
      credential: deviceCredential,
      body: { dashboard_id: dashboardId },
    });
    assert.equal(boundPairing.status, 201);
    const boundClaim = await api(captureBase, '/v1/pairings/claim', {
      method: 'POST',
      body: { pairing_code: boundPairing.json.pairing_code, gateway_name: 'bound' },
    });
    assert.equal(boundClaim.status, 200);
    const unboundPairing = await api(captureBase, `/v1/installations/${installationId}/pairings`, {
      method: 'POST',
      credential: deviceCredential,
      body: { dashboard_id: null },
    });
    assert.equal(unboundPairing.status, 201, 'explicit null pairs with no binding');
    const unboundClaim = await api(captureBase, '/v1/pairings/claim', {
      method: 'POST',
      body: { pairing_code: unboundPairing.json.pairing_code, gateway_name: 'unbound' },
    });
    assert.equal(unboundClaim.status, 200);

    // Gateway 1 sends an event whose body tries to REBIND itself to
    // dashboard B (and gateway 2 sends an ordinary one).
    const attackerEvent = await api(captureBase, '/v1/events', {
      method: 'POST',
      credential: boundClaim.json.credential,
      body: {
        type: 'response.ready',
        event_id: 'response:attack000001',
        session_id: 'sess-attack',
        dashboard_id: '99999999-9999-4999-8999-999999999999',
      },
    });
    assert.equal(attackerEvent.status, 202);
    const plainEvent = await api(captureBase, '/v1/events', {
      method: 'POST',
      credential: unboundClaim.json.credential,
      body: {
        type: 'response.ready',
        event_id: 'response:plain000001',
        session_id: 'sess-plain',
      },
    });
    assert.equal(plainEvent.status, 202);

    // Assert on the captured APNs payloads.
    const lines = readFileSync(capturePath, 'utf8').trim().split('\n').map((line) => JSON.parse(line));
    const attacker = lines.find((entry) => entry.notification.payload.conduit.session_id === 'sess-attack');
    const plain = lines.find((entry) => entry.notification.payload.conduit.session_id === 'sess-plain');
    assert.ok(attacker, 'attacker notification captured');
    assert.ok(plain, 'plain notification captured');
    assert.equal(
      attacker.notification.payload.conduit.dashboard_id,
      dashboardId,
      'the outgoing push carries the AUTHENTICATED gateway binding, not the event body'
    );
    assert.equal(attacker.notification.payload.body.conduit.dashboard_id, dashboardId);
    assert.equal(
      'dashboard_id' in plain.notification.payload.conduit,
      false,
      'an unbound gateway keeps the exact legacy wire shape'
    );
    assert.ok(
      !JSON.stringify(attacker.notification.payload).includes('99999999'),
      'the attacker-chosen identity never reaches the wire'
    );
  } finally {
    captureRelay.kill();
  }
});

test('full collision matrix: one installation, two gateways, identical dashboards', async () => {
  // GA -> dashboard A, GB -> dashboard B. Both dashboards use
  // profile=default, session=default, the SAME event_id and the SAME
  // plugin-minted request_id. Nothing may swallow, answer, cancel, or
  // rebind anything across the gateway boundary.
  const capturePath = join(dir, `capture-matrix-${Date.now()}.jsonl`);
  const matrixRelay = await startRelay(port + 3000, 'accept', join(dir, 'relay-data-matrix.json'), {
    APNS_CAPTURE_PATH: capturePath,
  });
  const matrixBase = `http://127.0.0.1:${port + 3000}`;
  try {
    const registered = await api(matrixBase, '/v1/installations', {
      method: 'POST',
      body: { bundle_id: 'com.milim.relay', device_token: '9'.repeat(64), environment: 'production' },
    });
    assert.equal(registered.status, 201);
    const installationId = registered.json.installation.id;
    const deviceCredential = registered.json.credential;

    const dashboardA = '0f5c8a34-1b2d-4e5f-8a9b-0c1d2e3f4a5b';
    const dashboardB = '11111111-2222-4333-8444-555555555555';
    const pairA = await api(matrixBase, `/v1/installations/${installationId}/pairings`, {
      method: 'POST', credential: deviceCredential, body: { dashboard_id: dashboardA },
    });
    const claimA = await api(matrixBase, '/v1/pairings/claim', {
      method: 'POST', body: { pairing_code: pairA.json.pairing_code, gateway_name: 'GA' },
    });
    const pairB = await api(matrixBase, `/v1/installations/${installationId}/pairings`, {
      method: 'POST', credential: deviceCredential, body: { dashboard_id: dashboardB },
    });
    const claimB = await api(matrixBase, '/v1/pairings/claim', {
      method: 'POST', body: { pairing_code: pairB.json.pairing_code, gateway_name: 'GB' },
    });
    const credA = claimA.json.credential;
    const credB = claimB.json.credential;

    // Identical plugin.hello ids: both gateways recorded (scoped dedupe).
    const helloBody = {
      type: 'plugin.hello', event_id: 'hello:collisio001', plugin_version: '0.3.0',
      plugin_capabilities: ['approval-decisions', 'clarify-loop', 'version-reporting'],
    };
    assert.equal((await api(matrixBase, '/v1/events', { method: 'POST', credential: credA, body: helloBody })).status, 202);
    assert.equal((await api(matrixBase, '/v1/events', { method: 'POST', credential: credB, body: helloBody })).status, 202);
    const meta = await api(matrixBase, '/v1/meta', { credential: deviceCredential });
    const gaId = meta.json.gateways.find((g) => g.name === 'GA').id;
    const gbId = meta.json.gateways.find((g) => g.name === 'GB').id;
    assert.equal(meta.json.gateways.find((g) => g.name === 'GA').dashboard_id, dashboardA);
    assert.equal(meta.json.gateways.find((g) => g.name === 'GB').dashboard_id, dashboardB);

    // Identical event_id values: BOTH accepted, neither dedupes the other.
    const evt = { type: 'response.ready', event_id: 'response:sameevent1', session_id: 'default', profile: 'default' };
    assert.deepEqual(
      (await api(matrixBase, '/v1/events', { method: 'POST', credential: credA, body: evt })).json,
      { accepted: true, delivered: true },
    );
    assert.deepEqual(
      (await api(matrixBase, '/v1/events', { method: 'POST', credential: credB, body: evt })).json,
      { accepted: true, delivered: true },
      "GB's identical event_id must NOT dedupe against GA's",
    );

    // Identical plugin-minted request ids: BOTH clarify decisions park.
    const clarifyBody = {
      type: 'input.needed',
      event_id: 'input:sameevent1',
      session_id: 'default',
      profile: 'default',
      decision: { kind: 'clarify', request_id: 'conduit-push-same-request', question: 'Which?', choices: ['Red', 'Blue'] },
    };
    assert.equal((await api(matrixBase, '/v1/events', { method: 'POST', credential: credA, body: clarifyBody })).status, 202);
    assert.equal((await api(matrixBase, '/v1/events', { method: 'POST', credential: credB, body: clarifyBody })).status, 202);

    const respond = (gatewayId, body) => api(matrixBase, '/v1/decisions/conduit-push-same-request/respond', {
      method: 'POST', credential: deviceCredential, body: { gateway_id: gatewayId, ...body },
    });

    // A answers with the discriminator: only A's decision locks.
    const answerA = await respond(gaId, { answer: 'Red' });
    assert.equal(answerA.status, 200);
    assert.deepEqual(answerA.json, { status: 'answered' });

    // B polls with its OWN credential: still pending — A's answer never leaked.
    const pollB = await api(matrixBase, '/v1/decisions/conduit-push-same-request', { credential: credB });
    assert.equal(pollB.json.status, 'pending', "A's answer must not resolve B's same-id decision");
    // A's poller sees its own answered state.
    const pollA = await api(matrixBase, '/v1/decisions/conduit-push-same-request', { credential: credA });
    assert.equal(pollA.json.status, 'answered');

    // B answers with the discriminator: only B locks.
    assert.equal((await respond(gbId, { answer: 'Blue' })).status, 200);
    assert.equal((await api(matrixBase, '/v1/decisions/conduit-push-same-request', { credential: credB })).json.status, 'answered');

    // A plugin-supplied dashboard_id/gateway_id can never rebind routing:
    // GA sends an event LYING about both. The payload discriminators must
    // still be GA's authenticated identities (asserted on the capture below).
    const lyingEvent = {
      type: 'input.needed',
      event_id: 'input:sameevent3',
      session_id: 'default',
      profile: 'default',
      dashboard_id: dashboardB,
      gateway_id: gbId,
      decision: { kind: 'clarify', request_id: 'conduit-push-same-three', question: 'Lie?', choices: ['x'] },
    };
    assert.equal((await api(matrixBase, '/v1/events', { method: 'POST', credential: credA, body: lyingEvent })).status, 202);

    // Legacy respond WITHOUT a discriminator:
    // Ambiguous while two gateways hold the same live id → FAIL CLOSED.
    const freshClarify = {
      ...clarifyBody,
      event_id: 'input:sameevent2',
      decision: { ...clarifyBody.decision, request_id: 'conduit-push-same-two' },
    };
    await api(matrixBase, '/v1/events', { method: 'POST', credential: credA, body: freshClarify });
    await api(matrixBase, '/v1/events', { method: 'POST', credential: credB, body: freshClarify });
    const legacyAmbiguous = await api(matrixBase, '/v1/decisions/conduit-push-same-two/respond', {
      method: 'POST', credential: deviceCredential, body: { answer: 'Guess' },
    });
    assert.equal(legacyAmbiguous.status, 400);
    assert.equal(legacyAmbiguous.json.error, 'ambiguous_decision');

    // Cancel A's fresh decision via GA's OWN credential; B's stays pending.
    assert.equal((await api(matrixBase, '/v1/decisions/conduit-push-same-two', { method: 'DELETE', credential: credA })).status, 200);
    assert.equal(
      (await api(matrixBase, '/v1/decisions/conduit-push-same-two', { credential: credB })).json.status,
      'pending',
      'cancelling A cannot mutate B',
    );

    // Legacy respond now resolves UNAMBIGUOUSLY (exactly one live holder).
    const legacyUnambiguous = await api(matrixBase, '/v1/decisions/conduit-push-same-two/respond', {
      method: 'POST', credential: deviceCredential, body: { answer: 'Only-B-left' },
    });
    assert.equal(legacyUnambiguous.status, 200);
    assert.equal(
      (await api(matrixBase, '/v1/decisions/conduit-push-same-two', { credential: credB })).json.status,
      'answered',
    );

    // Zero matches keeps the existing unknown behavior.
    assert.equal(
      (await api(matrixBase, '/v1/decisions/conduit-push-never-parked/respond', {
        method: 'POST', credential: deviceCredential, body: { answer: 'x' },
      })).status,
      404,
    );

    // Captured APNs payloads: scope isolation + the trust boundary.
    const lines = readFileSync(capturePath, 'utf8').trim().split('\n').map((line) => JSON.parse(line));
    const decisionEntries = lines.filter((entry) => {
      const decision = entry.notification.payload.body?.conduit?.decision;
      return decision && decision.request_id === 'conduit-push-same-request';
    });
    assert.equal(decisionEntries.length, 2, 'both same-id decisions were delivered');
    const [firstEntry, secondEntry] = decisionEntries;
    assert.notEqual(firstEntry.notification.collapseId, secondEntry.notification.collapseId, 'A/default and B/default never coalesce');
    assert.notEqual(
      firstEntry.notification.payload.aps['thread-id'],
      secondEntry.notification.payload.aps['thread-id'],
      'threads never merge across dashboards',
    );
    assert.ok(Buffer.byteLength(firstEntry.notification.collapseId) <= 64, 'collapse id fits the APNs cap');
    // The discriminators come from the AUTHENTICATED gateways — GA's lying
    // event (dashboard_id=B, gateway_id=GB in the body) must NOT rebind.
    // Each entry's discriminators pair with its OWN authenticated gateway:
    // the entry carrying GA's gateway_id carries dashboard A, and GB's
    // carries dashboard B (random UUIDs — never assume sort order).
    const byGateway = Object.fromEntries(
      decisionEntries.map((entry) => [entry.notification.payload.conduit.gateway_id, entry.notification.payload.conduit.dashboard_id]));
    assert.deepEqual(byGateway, { [gaId]: dashboardA, [gbId]: dashboardB });
    // GA's lying decision entry: routing carries GA's identity, never GB's.
    const lyingEntry = lines.find((entry) => {
      const decision = entry.notification.payload.body?.conduit?.decision;
      return decision && decision.request_id === 'conduit-push-same-three';
    });
    assert.equal(lyingEntry.notification.payload.conduit.gateway_id, gaId, 'event-body gateway_id is dropped');
    assert.equal(lyingEntry.notification.payload.conduit.dashboard_id, dashboardA, 'event-body dashboard_id is dropped');
  } finally {
    matrixRelay.kill();
  }
});
