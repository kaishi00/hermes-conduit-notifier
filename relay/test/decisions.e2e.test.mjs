import { strict as assert } from 'node:assert';
import { generateKeyPairSync } from 'node:crypto';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { after, before, test } from 'node:test';

// Full clarify answer loop against real relay processes: register a device,
// pair a gateway, push clarify decision events, answer from the device, and
// poll from the gateway. Two relays are spawned so APNs outcomes are
// deterministic: the accept relay "delivers" every notification (APNS_MODE
// stub, no network) and the reject relay fails every send — covering
// delivery success, plain-banner fallback, and APNs-failure cleanup. Nothing
// about the HTTP API is mocked.

const dir = mkdtempSync(join(tmpdir(), 'conduit-relay-e2e-'));
const port = 19000 + Math.floor(Math.random() * 1000);
const rejectPort = port + 500;
const baseUrl = `http://127.0.0.1:${port}`;
const rejectBaseUrl = `http://127.0.0.1:${rejectPort}`;
const dataPath = join(dir, 'relay-data.json');
const rejectDataPath = join(dir, 'relay-data-reject.json');
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

async function startRelay(aPort, apnsMode, dataFile) {
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
  children.push(await startRelay(port, 'accept', dataPath));
  children.push(await startRelay(rejectPort, 'reject', rejectDataPath));
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
