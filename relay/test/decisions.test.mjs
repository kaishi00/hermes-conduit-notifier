import { strict as assert } from 'node:assert';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { after, test } from 'node:test';

import { notificationFor } from '../src/server.mjs';
import { RelayStore } from '../src/store.mjs';

const dir = mkdtempSync(join(tmpdir(), 'conduit-relay-decisions-'));
after(() => rmSync(dir, { recursive: true, force: true }));

function store(limits) {
  return new RelayStore(join(dir, `store-${Math.random().toString(36).slice(2)}.json`), limits);
}

test('storage limits must be positive safe integers before store files are touched', () => {
  const invalidValues = [0, -1, Number.NaN, 1.5, Number.MAX_SAFE_INTEGER + 1];
  const limitNames = [
    'maxInstallations',
    'maxGatewaysPerInstallation',
    'maxEventIdsPerInstallation',
    'maxGlobalEventIds',
  ];

  for (const limitName of limitNames) {
    for (const invalidValue of invalidValues) {
      const path = join(dir, `invalid-${limitName}-${Math.random().toString(36).slice(2)}.json`);
      assert.throws(
        () => new RelayStore(path, { [limitName]: invalidValue }),
        /Storage limits must be positive integers\./,
        `${limitName}=${String(invalidValue)} must be rejected`,
      );
      assert.equal(existsSync(path), false, 'invalid limits must be rejected before loading or creating store state');
    }
  }
});

test('registration with null preferences uses the default preferences', () => {
  const relay = store();
  const created = relay.createInstallation({
    bundleId: 'com.milim.relay', deviceToken: 'n'.repeat(64), environment: 'production', preferences: null,
  });

  assert.equal(created.installation.preferences.enabled, true);
  assert.equal(created.installation.preferences.approval_needed, true);
  assert.equal(created.installation.preferences.show_previews, false);
  assert.equal(created.installation.preferences.decision_cards, true);
});

test('loading and pruning malformed pairings removes corrupt entries and preserves a valid future pairing', () => {
  const path = join(dir, `malformed-pairings-${Math.random().toString(36).slice(2)}.json`);
  const validKey = 'valid-future';
  writeFileSync(path, JSON.stringify({
    version: 1,
    installations: {},
    pairings: {
      nullRecord: null,
      stringRecord: 'corrupt',
      invalidDate: { expiresAt: 'not-a-date' },
      expired: { expiresAt: new Date(Date.now() - 60_000).toISOString() },
      [validKey]: { installationId: 'inst-future', expiresAt: new Date(Date.now() + 60_000).toISOString() },
    },
    eventIds: {},
    pendingDecisions: {},
  }));

  const relay = new RelayStore(path);

  assert.deepEqual(Object.keys(relay.data.pairings), [validKey]);
  assert.equal(relay.data.pairings[validKey].installationId, 'inst-future');
});

test('same-id decisions from two gateways coexist and never mutate each other', () => {
  // Gateway-scoped ownership: two gateways on one installation (identical
  // dashboards minting identical plugin request ids) park, answer, cancel,
  // and fail delivery INDEPENDENTLY.
  const relay = store();
  const shared = 'conduit-push-x';
  relay.savePendingDecision({ id: shared, installationId: 'inst-1', gatewayId: 'gw-A', question: 'A?' });
  relay.savePendingDecision({ id: shared, installationId: 'inst-1', gatewayId: 'gw-B', question: 'B?' });
  relay.savePendingDecision({ id: shared, installationId: 'inst-2', gatewayId: 'gw-C', question: 'C?' });

  // All three coexist; B's save never overwrote A's question.
  assert.equal(relay.pendingDecisionStatus('inst-1', 'gw-A', shared).status, 'pending');
  assert.equal(relay.pendingDecisionStatus('inst-1', 'gw-B', shared).status, 'pending');
  assert.equal(relay.pendingDecisionStatus('inst-2', 'gw-C', shared).status, 'pending');

  // Answering A modifies only A.
  assert.equal(relay.respondPendingDecision('inst-1', 'gw-A', shared, 'Red').outcome, 'answered');
  assert.equal(relay.pendingDecisionStatus('inst-1', 'gw-A', shared).status, 'answered');
  assert.equal(relay.pendingDecisionStatus('inst-1', 'gw-B', shared).status, 'pending', "B's same-id decision stays pending");
  assert.equal(relay.pendingDecisionStatus('inst-2', 'gw-C', shared).status, 'pending', "another installation's same-id decision stays pending");
  assert.equal(relay.respondPendingDecision('inst-1', 'gw-B', shared, 'Blue').outcome, 'answered', 'B answers its OWN decision independently');
  assert.equal(relay.pendingDecisionStatus('inst-2', 'gw-C', shared).status, 'pending');

  // Cancel A cannot mutate B; marking A undeliverable cannot either.
  assert.equal(relay.cancelPendingDecision('inst-1', 'gw-B', shared), 'answered', 'cancelling an answered decision reports it');
  assert.equal(relay.cancelPendingDecision('inst-1', 'gw-A', shared), 'answered');
  assert.equal(relay.cancelPendingDecision('inst-1', 'gw-B', shared), 'answered');
  assert.equal(relay.markPendingDecisionUndeliverable('inst-1', 'gw-A', shared), 'skipped', 'completed decisions are never re-marked');
  assert.equal(relay.pendingDecisionStatus('inst-1', 'gw-B', shared).status, 'answered');

  // Cross-gateway respond with a WRONG gateway id resolves nothing.
  assert.equal(relay.respondPendingDecision('inst-1', 'gw-nope', shared, 'x').outcome, 'unknown');

  // Legacy resolution: both holders settled (answered) → already_answered,
  // never a guess between gateways.
  assert.deepEqual(relay.resolveLegacyRespond('inst-1', shared), { resolution: 'already_answered' });
  // Fresh same-id decisions on both gateways: two live → ambiguous.
  const sharedAgain = `${shared}-again`;
  relay.savePendingDecision({ id: sharedAgain, installationId: 'inst-1', gatewayId: 'gw-A', question: 'A2?' });
  relay.savePendingDecision({ id: sharedAgain, installationId: 'inst-1', gatewayId: 'gw-B', question: 'B2?' });
  assert.deepEqual(relay.resolveLegacyRespond('inst-1', sharedAgain), { resolution: 'ambiguous' });
  // Cancelling one leaves exactly one live holder → unique, and one
  // released holder means a settled legacy answer reports released.
  assert.equal(relay.cancelPendingDecision('inst-1', 'gw-A', sharedAgain), 'cancelled');
  assert.deepEqual(relay.resolveLegacyRespond('inst-1', sharedAgain), { resolution: 'unique', gatewayId: 'gw-B' });
  assert.equal(relay.cancelPendingDecision('inst-1', 'gw-B', sharedAgain), 'cancelled');
  assert.deepEqual(relay.resolveLegacyRespond('inst-1', sharedAgain), { resolution: 'released' });
  // Unknown id → unknown.
  assert.deepEqual(relay.resolveLegacyRespond('inst-1', 'conduit-push-never'), { resolution: 'unknown' });
});

test('event IDs from an older data file keep deduping and leave the file on the next save', () => {
  const path = join(dir, `legacy-events-${Math.random().toString(36).slice(2)}.json`);
  writeFileSync(path, JSON.stringify({
    version: 1,
    installations: {},
    pairings: {},
    eventIds: {
      'inst:gw:old-number': Date.now() - 25 * 60 * 60_000,
      'inst:gw:nan': 'not-a-time',
      'inst:gw:infinity': Infinity,
      'inst:gw:plugin:valid': Date.now(),
      'malformed-key': Date.now(),
    },
    pendingDecisions: {},
  }));

  const relay = new RelayStore(path);

  assert.equal(relay.hasAcceptedEvent('inst', 'plugin:valid', 'gw'), true, 'event IDs keep their colons after the gateway');
  assert.equal(relay.acceptEvent('inst', 'plugin:valid', 'gw'), false);
  for (const eventId of ['old-number', 'nan', 'infinity']) assert.equal(relay.hasAcceptedEvent('inst', eventId, 'gw'), false);
  assert.equal(relay.events.size, 1);
  relay.save();
  assert.equal(JSON.parse(readFileSync(path, 'utf8')).eventIds, undefined);
});

test('a failed save keeps its changes pending and retries after the save delay', async () => {
  const path = join(dir, `retry-${Math.random().toString(36).slice(2)}.json`);
  const relay = new RelayStore(path, {}, { saveDelayMs: 20 });
  const created = relay.createInstallation({ bundleId: 'app', deviceToken: 'a'.repeat(64), environment: 'production' });
  const blocker = join(dir, `not-a-directory-${Math.random().toString(36).slice(2)}`);
  writeFileSync(blocker, '');
  relay.path = join(blocker, 'relay.json');
  const errors = [];
  const consoleError = console.error;
  console.error = (line) => errors.push(line);
  try {
    assert.throws(() => relay.deactivateInstallation(created.installation.id));
    assert.equal(relay.dirty, true, 'the unsaved change stays pending');
    await new Promise((resolve) => setTimeout(resolve, 60));
    assert.ok(errors.some((line) => line.includes('deferred relay save failed')), 'the retry ran and was logged');
    relay.path = path;
    await new Promise((resolve) => setTimeout(resolve, 60));
  } finally {
    console.error = consoleError;
  }
  assert.equal(relay.dirty, false);
  assert.equal(JSON.parse(readFileSync(path, 'utf8')).installations[created.installation.id].active, false, 'a later retry wrote the change');
});

test('a failed save removes its temporary file', () => {
  const path = join(dir, `temporary-${Math.random().toString(36).slice(2)}.json`);
  const relay = new RelayStore(path, {}, { saveDelayMs: 60_000 });
  const created = relay.createInstallation({ bundleId: 'app', deviceToken: 'a'.repeat(64), environment: 'production' });
  // Renaming a file over a directory fails after the temporary file is written.
  const directoryTarget = join(dir, `directory-${Math.random().toString(36).slice(2)}`);
  mkdirSync(directoryTarget);
  relay.path = directoryTarget;
  assert.throws(() => relay.deactivateInstallation(created.installation.id));
  assert.equal(existsSync(`${directoryTarget}.${process.pid}.tmp`), false);
  relay.path = path;
  relay.flush();
  assert.equal(relay.dirty, false);
  assert.equal(JSON.parse(readFileSync(path, 'utf8')).installations[created.installation.id].active, false);
});

test('event metadata is written by a deferred save, and an immediate save includes it', async () => {
  const path = join(dir, `deferred-${Math.random().toString(36).slice(2)}.json`);
  const relay = new RelayStore(path, {}, { saveDelayMs: 20 });
  const created = relay.createInstallation({ bundleId: 'app', deviceToken: 'a'.repeat(64), environment: 'production' });
  const claim = relay.claimPairing(relay.createPairing(created.installation.id).code, 'gateway');
  const persistedGateway = () => JSON.parse(readFileSync(path, 'utf8')).installations[created.installation.id].gateways[claim.gatewayId];
  let writes = 0;
  const save = relay.save.bind(relay);
  relay.save = () => { writes += 1; save(); };

  assert.equal(relay.acceptEvent(created.installation.id, 'response:1', claim.gatewayId), true);
  relay.recordGatewayPlugin(created.installation.id, claim.gatewayId, { version: '1.2.3' });
  assert.equal(writes, 0, 'accepting an event does not rewrite the data file');
  assert.equal(persistedGateway().pluginVersion, undefined);
  await new Promise((resolve) => setTimeout(resolve, 60));
  assert.equal(writes, 1, 'repeated metadata changes collapse into one write');
  assert.equal(persistedGateway().pluginVersion, '1.2.3');
  assert.ok(persistedGateway().lastEventAt);

  relay.recordGatewayPlugin(created.installation.id, claim.gatewayId, { version: '1.2.4' });
  relay.deactivateInstallation(created.installation.id);
  assert.equal(persistedGateway().pluginVersion, '1.2.4', 'a full save writes pending metadata too');
  await new Promise((resolve) => setTimeout(resolve, 60));
  assert.equal(writes, 2, 'the full save cancelled the deferred one');
  relay.flush();
  assert.equal(writes, 2, 'flush only writes pending changes');
});

test('installation update preflight compares preference meaning, not key order', () => {
  const relay = store();
  const created = relay.createInstallation({
    bundleId: 'com.milim.relay', deviceToken: 'a'.repeat(64), environment: 'production',
    preferences: { enabled: false, show_previews: true },
  });
  const installation = relay.data.installations[created.installation.id];
  installation.preferences = Object.fromEntries(Object.entries(installation.preferences).reverse());
  relay.save();
  const before = readFileSync(relay.path, 'utf8');

  assert.equal(relay.wouldUpdateInstallation(created.installation.id, {
    preferences: { show_previews: true, enabled: false },
  }), false);
  const updated = relay.updateInstallation(created.installation.id, {
    preferences: { show_previews: true, enabled: false },
  });

  assert.deepEqual(updated.preferences, created.installation.preferences);
  assert.equal(readFileSync(relay.path, 'utf8'), before, 'semantic no-op does not rewrite stored state');
});

test('installation update preflight preserves explicit enablement for legacy preferences', () => {
  const relay = store();
  const created = relay.createInstallation({
    bundleId: 'com.milim.relay', deviceToken: 'b'.repeat(64), environment: 'production', preferences: {},
  });
  relay.data.installations[created.installation.id].preferences = {};
  relay.save();

  const changes = { preferences: { enabled: true } };
  assert.equal(relay.wouldUpdateInstallation(created.installation.id, changes), true);
  const updated = relay.updateInstallation(created.installation.id, changes);

  assert.equal(updated.preferences.enabled, true);
  assert.equal(relay.data.installations[created.installation.id].preferences.enabled, true);
});

test('response preflight matches scalar, batch-first, duplicate, invalid, released, and unknown outcomes', () => {
  const relay = store();
  relay.savePendingDecision({ id: 'conduit-push-scalar', installationId: 'inst-r', gatewayId: 'gw-r', question: 'Scalar?' });
  relay.savePendingDecision({ id: 'conduit-push-answered', installationId: 'inst-r', gatewayId: 'gw-r', question: 'Done?' });
  relay.respondPendingDecision('inst-r', 'gw-r', 'conduit-push-answered', 'yes');
  relay.savePendingDecision({ id: 'conduit-push-batch-preflight', installationId: 'inst-r', gatewayId: 'gw-r', question: 'First?', questions: [
    { qid: 'q0', question: 'First?', choices: ['a'] },
    { qid: 'q1', question: 'Second?', choices: ['b'] },
  ] });
  relay.respondPendingDecision('inst-r', 'gw-r', 'conduit-push-batch-preflight', 'a', 'q0');
  relay.savePendingDecision({ id: 'conduit-push-released', installationId: 'inst-r', gatewayId: 'gw-r', question: 'Released?' });
  relay.cancelPendingDecision('inst-r', 'gw-r', 'conduit-push-released');
  relay.savePendingDecision({ id: 'conduit-push-corrupt-time', installationId: 'inst-r', gatewayId: 'gw-r', question: 'Corrupt time?' });
  relay.data.pendingDecisions[RelayStore.decisionKey('inst-r', 'gw-r', 'conduit-push-corrupt-time')].createdAt = Number.NaN;
  const before = JSON.stringify(relay.data.pendingDecisions);

  assert.equal(relay.wouldRespondPendingDecision('inst-r', 'gw-r', 'conduit-push-scalar', 'ignored-qid'), true, 'scalar responses ignore question ids');
  assert.equal(relay.wouldRespondPendingDecision('inst-r', 'gw-r', 'conduit-push-answered'), false);
  assert.equal(relay.wouldRespondPendingDecision('inst-r', 'gw-r', 'conduit-push-batch-preflight'), false, 'omitted qid targets the already-answered first batch question');
  assert.equal(relay.wouldRespondPendingDecision('inst-r', 'gw-r', 'conduit-push-batch-preflight', 'q0'), false);
  assert.equal(relay.wouldRespondPendingDecision('inst-r', 'gw-r', 'conduit-push-batch-preflight', 'q1'), true);
  assert.equal(relay.wouldRespondPendingDecision('inst-r', 'gw-r', 'conduit-push-batch-preflight', 'q9'), false);
  assert.equal(relay.wouldRespondPendingDecision('inst-r', 'gw-r', 'conduit-push-released'), false);
  assert.equal(relay.wouldRespondPendingDecision('inst-r', 'gw-r', 'conduit-push-missing'), false);
  assert.equal(relay.wouldRespondPendingDecision('inst-r', 'gw-r', 'conduit-push-corrupt-time'), false);
  assert.equal(JSON.stringify(relay.data.pendingDecisions), before, 'preflight reads state without mutating it');
});

test('event dedupe is gateway-scoped: same event id from different gateways both accepted', () => {
  const relay = store();
  assert.equal(relay.acceptEvent('inst-1', 'approval:42', 'gw-A'), true);
  // Same gateway + same id → duplicate.
  assert.equal(relay.acceptEvent('inst-1', 'approval:42', 'gw-A'), false);
  // Different gateway, identical id → accepted: one dashboard's event must
  // never swallow another's.
  assert.equal(relay.acceptEvent('inst-1', 'approval:42', 'gw-B'), true);
  // Third installation, same gateway-id and event id → accepted.
  assert.equal(relay.acceptEvent('inst-2', 'approval:42', 'gw-A'), true);
});

test('installation and gateway quotas reject new records without evicting existing credentials', () => {
  const relay = new RelayStore(join(dir, `limits-${Math.random().toString(36).slice(2)}.json`), {
    maxInstallations: 1, maxGatewaysPerInstallation: 1, maxEventIdsPerInstallation: 1,
  });
  const first = relay.createInstallation({ bundleId: 'app', deviceToken: 'a'.repeat(64), environment: 'production' });
  const pairing = relay.createPairing(first.installation.id);
  const claim = relay.claimPairing(pairing.code, 'first');
  assert.throws(() => relay.createInstallation({ bundleId: 'app', deviceToken: 'b'.repeat(64), environment: 'production' }), /installation_limit_reached/);
  const secondPairing = relay.createPairing(first.installation.id);
  assert.throws(() => relay.claimPairing(secondPairing.code, 'second'), /gateway_limit_reached/);
  assert.equal(Object.keys(relay.data.pairings).length, 1, 'a rejected claim keeps its pairing available');
  assert.ok(relay.authenticate(first.installation.id, first.deviceSecret, 'device'));
  assert.ok(relay.authenticateGateway(first.installation.id, claim.gatewayId, claim.gatewaySecret));
  assert.equal(relay.acceptEvent(first.installation.id, 'event-0001', claim.gatewayId), true);
  assert.equal(relay.acceptEvent(first.installation.id, 'event-0002', claim.gatewayId), true, 'a full event ledger forgets instead of rejecting');
  assert.equal(relay.acceptEvent(first.installation.id, 'event-0002', claim.gatewayId), false);
  relay.deactivateInstallation(first.installation.id);
  assert.equal(relay.data.installations[first.installation.id].active, false);
  assert.throws(() => relay.createInstallation({ bundleId: 'app', deviceToken: 'c'.repeat(64), environment: 'production' }), /installation_limit_reached/);
});

test('loading a store already above a configured quota preserves all existing records', () => {
  const path = join(dir, `legacy-limit-${Math.random().toString(36).slice(2)}.json`);
  const installations = Object.fromEntries(['first', 'second'].map((id) => [id, { id, active: true, deviceSecretHash: id, gateways: {} }]));
  writeFileSync(path, JSON.stringify({ version: 1, installations, pairings: {}, eventIds: {}, pendingDecisions: {} }));
  const relay = new RelayStore(path, { maxInstallations: 1 });
  assert.deepEqual(Object.keys(relay.data.installations), ['first', 'second']);
  assert.deepEqual(JSON.parse(readFileSync(path, 'utf8')).installations, installations);
  assert.throws(() => relay.createInstallation({ bundleId: 'app', deviceToken: 'c'.repeat(64), environment: 'production' }), /installation_limit_reached/);
});

test('event-ID bounds forget the heaviest installation first and never reject an event', () => {
  const relay = new RelayStore(join(dir, `global-events-${Math.random().toString(36).slice(2)}.json`), {
    maxEventIdsPerInstallation: 2, maxGlobalEventIds: 3,
  });
  assert.equal(relay.acceptEvent('inst-a', 'a1', 'gw-a'), true);
  assert.equal(relay.acceptEvent('inst-a', 'a2', 'gw-a'), true);
  assert.equal(relay.acceptEvent('inst-a', 'a3', 'gw-a'), true, 'an installation at its bound forgets its own oldest ID');
  assert.equal(relay.hasAcceptedEvent('inst-a', 'a1', 'gw-a'), false);
  assert.equal(relay.acceptEvent('inst-b', 'b1', 'gw-b'), true);
  assert.equal(relay.acceptEvent('inst-c', 'c1', 'gw-c'), true, 'the relay-wide bound forgets instead of rejecting');
  assert.equal(relay.hasAcceptedEvent('inst-a', 'a2', 'gw-a'), false, 'the installation holding the most IDs gives one up');
  assert.equal(relay.hasAcceptedEvent('inst-a', 'a3', 'gw-a'), true);
  assert.equal(relay.hasAcceptedEvent('inst-b', 'b1', 'gw-b'), true, 'lighter installations keep their IDs');
  assert.equal(relay.hasAcceptedEvent('inst-c', 'c1', 'gw-c'), true);
  assert.equal(relay.events.size, 3);
  assert.equal(relay.acceptEvent('inst-a', 'a3', 'gw-replacement'), true, 'dedupe stays gateway-scoped');

  relay.events.add('inst-d', 'gw-d', 'stale', Date.now() - 24 * 60 * 60_000 - 1);
  assert.equal(relay.hasAcceptedEvent('inst-d', 'stale', 'gw-d'), false, 'IDs older than 24 hours no longer count');
  assert.equal(relay.acceptEvent('inst-d', 'stale', 'gw-d'), true);
});

test('no-op installation updates and repeated cancellation do not save or rewrite state', () => {
  const relay = store();
  const created = relay.createInstallation({ bundleId: 'app', deviceToken: 'a'.repeat(64), environment: 'production' });
  relay.savePendingDecision({ id: 'conduit-push-cancel', installationId: created.installation.id, gatewayId: 'gw-1', question: 'q' });
  const installation = relay.data.installations[created.installation.id];
  const updatedAt = installation.updatedAt;
  let writes = 0;
  relay.save = () => { writes += 1; };

  assert.deepEqual(relay.updateInstallation(installation.id, { deviceToken: installation.deviceToken, preferences: installation.preferences }), {
    id: installation.id, active: true, gateways: [], preferences: installation.preferences, voip: false, watch_voip: false, updated_at: updatedAt,
  });
  assert.equal(relay.cancelPendingDecision(installation.id, 'gw-1', 'conduit-push-cancel'), 'cancelled');
  const cancelledAt = relay.data.pendingDecisions[RelayStore.decisionKey(installation.id, 'gw-1', 'conduit-push-cancel')].cancelledAt;
  assert.equal(relay.cancelPendingDecision(installation.id, 'gw-1', 'conduit-push-cancel'), 'cancelled');
  assert.equal(relay.data.pendingDecisions[RelayStore.decisionKey(installation.id, 'gw-1', 'conduit-push-cancel')].cancelledAt, cancelledAt);
  assert.equal(writes, 1, 'only the first cancellation persists');
});

test('pending decision lifecycle: save → pending → answered → already', () => {
  const relay = store();
  relay.savePendingDecision({
    id: 'conduit-push-abc123',
    installationId: 'inst-1',
    gatewayId: 'gw-1',
    question: 'Which color?',
    choices: ['Red', 'Blue'],
  });

  assert.deepEqual(
    relay.pendingDecisionStatus('inst-1', 'gw-1', 'conduit-push-abc123'),
    { status: 'pending', deliverable: true },
  );
  // Deliverability is recorded at intake: a decision whose card device
  // preferences suppressed reports deliverable:false so the plugin's poll
  // can stop early.
  relay.savePendingDecision({
    id: 'conduit-push-hidden',
    installationId: 'inst-1',
    gatewayId: 'gw-1',
    question: 'q',
    deliverable: false,
  });
  assert.deepEqual(
    relay.pendingDecisionStatus('inst-1', 'gw-1', 'conduit-push-hidden'),
    { status: 'pending', deliverable: false },
  );
  // Legacy records without the flag default to deliverable.
  relay.data.pendingDecisions[RelayStore.decisionKey('inst-1', 'gw-1', 'conduit-push-hidden')].deliverable = undefined;
  assert.deepEqual(
    relay.pendingDecisionStatus('inst-1', 'gw-1', 'conduit-push-hidden'),
    { status: 'pending', deliverable: true },
  );
  // Cross-installation and cross-gateway reads never see it.
  assert.deepEqual(relay.pendingDecisionStatus('inst-2', 'gw-1', 'conduit-push-abc123'), { status: 'unknown' });
  assert.deepEqual(relay.pendingDecisionStatus('inst-1', 'gw-2', 'conduit-push-abc123'), { status: 'unknown' });
  assert.deepEqual(relay.pendingDecisionStatus('inst-1', 'gw-1', 'nope'), { status: 'unknown' });

  assert.equal(relay.respondPendingDecision('inst-1', 'gw-1', 'conduit-push-abc123', 'Red').outcome, 'answered');
  assert.deepEqual(
    relay.pendingDecisionStatus('inst-1', 'gw-1', 'conduit-push-abc123'),
    { status: 'answered', answer: 'Red' },
  );
  // A device from another installation cannot answer or re-answer.
  assert.equal(relay.respondPendingDecision('inst-2', 'gw-2', 'conduit-push-abc123', 'Blue').outcome, 'unknown');
  assert.equal(relay.respondPendingDecision('inst-1', 'gw-1', 'conduit-push-abc123', 'Blue').outcome, 'already_answered');
  assert.equal(relay.pendingDecisionStatus('inst-1', 'gw-1', 'conduit-push-abc123').answer, 'Red');
});

test('batch decisions accumulate per-question answers and complete on the last qid', () => {
  const relay = store();
  relay.savePendingDecision({
    id: 'conduit-push-batch1',
    installationId: 'inst-1',
    gatewayId: 'gw-1',
    question: 'Which environment?',
    choices: ['staging', 'prod'],
    questions: [
      { qid: 'q0', question: 'Which environment?', choices: ['staging', 'prod'], multi_select: false },
      { qid: 'q1', question: 'Which tests?', choices: ['unit', 'ui'], multi_select: true },
    ],
  });

  // While open, the poll reports the authoritative open-qid list.
  const pending = relay.pendingDecisionStatus('inst-1', 'gw-1', 'conduit-push-batch1');
  assert.equal(pending.status, 'pending');
  assert.deepEqual(pending.remaining, ['q0', 'q1']);

  // First answer locks ONLY its qid; the sibling stays open.
  const first = relay.respondPendingDecision('inst-1', 'gw-1', 'conduit-push-batch1', 'staging', 'q0');
  assert.equal(first.outcome, 'answered');
  assert.deepEqual(first.remaining, ['q1']);
  assert.equal(relay.pendingDecisionStatus('inst-1', 'gw-1', 'conduit-push-batch1').remaining.join(','), 'q1');

  // A concurrent device locking the same qid loses (first-answer-wins per qid).
  assert.equal(relay.respondPendingDecision('inst-1', 'gw-1', 'conduit-push-batch1', 'prod', 'q0').outcome, 'already_answered');

  // The final answer completes the batch with every locked answer.
  const last = relay.respondPendingDecision('inst-1', 'gw-1', 'conduit-push-batch1', '["unit"]', 'q1');
  assert.equal(last.outcome, 'answered');
  assert.deepEqual(last.remaining, []);
  const done = relay.pendingDecisionStatus('inst-1', 'gw-1', 'conduit-push-batch1');
  assert.equal(done.status, 'answered');
  // Spread: the store keeps answers on a null-prototype map (prototype-
  // pollution safety), and strict deep-equal compares prototypes.
  assert.deepEqual({ ...done.answers }, { q0: 'staging', q1: '["unit"]' });
  assert.deepEqual(done.remaining, []);
});

test('an unknown qid and cross-installation answers never resolve a batch', () => {
  const relay = store();
  relay.savePendingDecision({
    id: 'conduit-push-batch2',
    installationId: 'inst-1',
    gatewayId: 'gw-1',
    question: 'One?',
    questions: [{ qid: 'q0', question: 'One?', choices: ['a'], multi_select: false }],
  });
  assert.equal(relay.respondPendingDecision('inst-1', 'gw-1', 'conduit-push-batch2', 'a', 'q9').outcome, 'invalid_question',
    'an unknown qid on a live decision is a malformed request, not a missing decision');
  assert.equal(relay.respondPendingDecision('inst-2', 'gw-2', 'conduit-push-batch2', 'a', 'q0').outcome, 'unknown');
  assert.equal(relay.pendingDecisionStatus('inst-1', 'gw-1', 'conduit-push-batch2').status, 'pending');
});

test('a legacy whole-decision answer on a batch counts as the first question only', () => {
  const relay = store();
  relay.savePendingDecision({
    id: 'conduit-push-batch3',
    installationId: 'inst-1',
    gatewayId: 'gw-1',
    question: 'Which environment?',
    questions: [
      { qid: 'q0', question: 'Which environment?', choices: ['staging'], multi_select: false },
      { qid: 'q1', question: 'Which tests?', choices: ['unit'], multi_select: false },
    ],
  });
  const result = relay.respondPendingDecision('inst-1', 'gw-1', 'conduit-push-batch3', 'staging');
  assert.equal(result.outcome, 'answered');
  assert.deepEqual(result.remaining, ['q1'], 'A pre-batch device answers the collapsed copy; the batch stays open');
  assert.equal(relay.pendingDecisionStatus('inst-1', 'gw-1', 'conduit-push-batch3').status, 'pending');
});

test('releasing a decision rejects late device answers', () => {
  const relay = store();
  relay.savePendingDecision({
    id: 'conduit-push-batch4',
    installationId: 'inst-1',
    gatewayId: 'gw-1',
    question: 'One?',
    questions: [{ qid: 'q0', question: 'One?', choices: ['a'], multi_select: false }],
  });
  assert.equal(relay.cancelPendingDecision('inst-1', 'gw-1', 'conduit-push-batch4'), 'cancelled');
  // The poller sees unknown and falls back to the original clarify path.
  assert.deepEqual(relay.pendingDecisionStatus('inst-1', 'gw-1', 'conduit-push-batch4'), { status: 'unknown' });
  // A late device answer reports RELEASED, not merely qid-locked: Conduit
  // tears the whole pushed card down instead of settling one question.
  assert.equal(relay.respondPendingDecision('inst-1', 'gw-1', 'conduit-push-batch4', 'a', 'q0').outcome, 'released');
  // Cancelling an already-completed decision is reported, not an error.
  relay.savePendingDecision({
    id: 'conduit-push-batch5',
    installationId: 'inst-1',
    gatewayId: 'gw-1',
    question: 'One?',
    questions: [{ qid: 'q0', question: 'One?', choices: ['a'], multi_select: false }],
  });
  relay.respondPendingDecision('inst-1', 'gw-1', 'conduit-push-batch5', 'a', 'q0');
  assert.equal(relay.cancelPendingDecision('inst-1', 'gw-1', 'conduit-push-batch5'), 'answered');
  assert.equal(relay.cancelPendingDecision('inst-1', 'gw-1', 'nope'), 'unknown');
});

test('batch question lists are sanitized and bounded at intake', () => {
  const relay = store();
  relay.savePendingDecision({
    id: 'conduit-push-batch6',
    installationId: 'inst-1',
    gatewayId: 'gw-1',
    question: 'summary',
    questions: [
      { qid: 'q0', question: 'Kept', choices: ['a'], multi_select: true },
      { qid: '', question: 'No qid dropped' },
      { question: 'No qid dropped either' },
      'not-an-object',
      { qid: 'q7', question: 'x'.repeat(600), choices: Array.from({ length: 20 }, (_, i) => `c${i}`) },
    ],
  });
  const stored = relay.data.pendingDecisions[RelayStore.decisionKey('inst-1', 'gw-1', 'conduit-push-batch6')].questions;
  assert.equal(stored.length, 2);
  assert.equal(stored[0].multi_select, true, 'wire shape keeps snake_case multi_select');
  assert.equal(stored[1].question.length, 500);
  assert.equal(stored[1].choices.length, 8);
});

test('pending decisions survive a reload and expire past the TTL', () => {
  const path = join(dir, `store-${Math.random().toString(36).slice(2)}.json`);
  const first = new RelayStore(path);
  first.savePendingDecision({ id: 'conduit-push-old', installationId: 'inst-1', gatewayId: 'gw-1', question: 'q' });

  const reloaded = new RelayStore(path);
  assert.equal(reloaded.pendingDecisionStatus('inst-1', 'gw-1', 'conduit-push-old').status, 'pending');

  // Age the record past the 2h TTL directly, then let any access prune it.
  reloaded.data.pendingDecisions[RelayStore.decisionKey('inst-1', 'gw-1', 'conduit-push-old')].createdAt = Date.now() - 3 * 60 * 60_000;
  assert.deepEqual(reloaded.pendingDecisionStatus('inst-1', 'gw-1', 'conduit-push-old'), { status: 'unknown' });
});

test('pending decisions reject overflow without evicting another installation or overwriting existing ids', () => {
  const relay = store();
  for (let i = 0; i < 32; i += 1) {
    relay.savePendingDecision({ id: `conduit-push-${i}`, installationId: 'inst-1', gatewayId: 'gw-1', question: 'q' });
  }
  assert.throws(
    () => relay.savePendingDecision({ id: 'conduit-push-over', installationId: 'inst-1', gatewayId: 'gw-1', question: 'q' }),
    { code: 'decision_capacity_exceeded' },
  );
  assert.equal(relay.pendingDecisionStatus('inst-1', 'gw-1', 'conduit-push-0').status, 'pending');
  assert.equal(relay.pendingDecisionStatus('inst-1', 'gw-1', 'conduit-push-31').status, 'pending');
  assert.equal(Object.keys(relay.data.pendingDecisions).length, 32);

  // Reusing a logical ID preserves its first request, including after it is answered.
  relay.respondPendingDecision('inst-1', 'gw-1', 'conduit-push-0', 'first answer');
  relay.savePendingDecision({ id: 'conduit-push-0', installationId: 'inst-1', gatewayId: 'gw-1', question: 'replacement?' });
  assert.equal(relay.pendingDecisionStatus('inst-1', 'gw-1', 'conduit-push-0').answer, 'first answer');
  assert.equal(relay.data.pendingDecisions[RelayStore.decisionKey('inst-1', 'gw-1', 'conduit-push-0')].question, 'q');
});

test('answered and cancelled decisions release active capacity while remaining retained', () => {
  const relay = store({ activePerInstallation: 1, activeGlobal: 1, retainedPerInstallation: 4, retainedGlobal: 4 });
  relay.savePendingDecision({ id: 'conduit-push-settled-0', installationId: 'inst-settled', gatewayId: 'gw-1', question: 'q' });
  assert.equal(relay.respondPendingDecision('inst-settled', 'gw-1', 'conduit-push-settled-0', 'kept answer').outcome, 'answered');
  relay.savePendingDecision({ id: 'conduit-push-after-answer', installationId: 'inst-settled', gatewayId: 'gw-1', question: 'q' });
  assert.equal(relay.cancelPendingDecision('inst-settled', 'gw-1', 'conduit-push-after-answer'), 'cancelled');
  relay.savePendingDecision({ id: 'conduit-push-after-cancel', installationId: 'inst-settled', gatewayId: 'gw-1', question: 'q' });
  assert.equal(Object.keys(relay.data.pendingDecisions).length, 3);
  assert.deepEqual(relay.pendingDecisionStatus('inst-settled', 'gw-1', 'conduit-push-settled-0'), { status: 'answered', answer: 'kept answer' });
  assert.equal(relay.respondPendingDecision('inst-settled', 'gw-1', 'conduit-push-after-answer', 'late').outcome, 'released');
});

test('a partial batch remains active until its final question is answered', () => {
  const relay = store({ activePerInstallation: 1, activeGlobal: 1, retainedPerInstallation: 4, retainedGlobal: 4 });
  relay.savePendingDecision({ id: 'conduit-push-partial', installationId: 'inst-batch', gatewayId: 'gw-1', questions: [
    { qid: 'q0', question: 'First?', choices: ['a'] }, { qid: 'q1', question: 'Second?', choices: ['b'] },
  ] });
  relay.respondPendingDecision('inst-batch', 'gw-1', 'conduit-push-partial', 'a', 'q0');
  assert.throws(() => relay.savePendingDecision({ id: 'conduit-push-blocked', installationId: 'inst-batch', gatewayId: 'gw-1', question: 'q' }), { code: 'decision_capacity_exceeded' });
  assert.deepEqual(relay.pendingDecisionStatus('inst-batch', 'gw-1', 'conduit-push-partial').remaining, ['q1']);
  relay.respondPendingDecision('inst-batch', 'gw-1', 'conduit-push-partial', 'b', 'q1');
  assert.equal(relay.savePendingDecision({ id: 'conduit-push-after-batch', installationId: 'inst-batch', gatewayId: 'gw-1', question: 'q' }), true);
});

test('per-installation retained cap rejects without dropping locks or blocking another installation', () => {
  const limits = { activePerInstallation: 3, activeGlobal: 3, retainedPerInstallation: 2, retainedGlobal: 4 };
  const path = join(dir, `store-retained-${Math.random().toString(36).slice(2)}.json`);
  const relay = new RelayStore(path, limits);
  relay.savePendingDecision({ id: 'conduit-push-answer-lock', installationId: 'inst-retain', gatewayId: 'gw-1', question: 'q' });
  relay.respondPendingDecision('inst-retain', 'gw-1', 'conduit-push-answer-lock', 'preserved');
  relay.savePendingDecision({ id: 'conduit-push-cancel-lock', installationId: 'inst-retain', gatewayId: 'gw-1', question: 'q' });
  relay.cancelPendingDecision('inst-retain', 'gw-1', 'conduit-push-cancel-lock');
  assert.throws(() => relay.savePendingDecision({ id: 'conduit-push-retained-over', installationId: 'inst-retain', gatewayId: 'gw-1', question: 'q' }), { code: 'decision_capacity_exceeded', status: 429 });
  const reloaded = new RelayStore(path, limits);
  assert.deepEqual(reloaded.pendingDecisionStatus('inst-retain', 'gw-1', 'conduit-push-answer-lock'), { status: 'answered', answer: 'preserved' });
  assert.equal(reloaded.respondPendingDecision('inst-retain', 'gw-1', 'conduit-push-cancel-lock', 'late').outcome, 'released');
  assert.equal(reloaded.savePendingDecision({ id: 'conduit-push-other-install', installationId: 'inst-other', gatewayId: 'gw-1', question: 'q' }), true);
});

test('global retained cap counts settled records across installations', () => {
  const relay = store({ activePerInstallation: 4, activeGlobal: 4, retainedPerInstallation: 4, retainedGlobal: 2 });
  for (let i = 0; i < 2; i += 1) {
    const installationId = `inst-retained-${i}`;
    const id = `conduit-push-retained-${i}`;
    relay.savePendingDecision({ id, installationId, gatewayId: 'gw-1', question: 'q' });
    relay.respondPendingDecision(installationId, 'gw-1', id, `answer-${i}`);
  }
  assert.throws(() => relay.savePendingDecision({ id: 'conduit-push-global-retained-over', installationId: 'inst-retained-new', gatewayId: 'gw-1', question: 'q' }), { code: 'decision_capacity_exceeded', status: 429 });
  assert.deepEqual(relay.pendingDecisionStatus('inst-retained-0', 'gw-1', 'conduit-push-retained-0'), { status: 'answered', answer: 'answer-0' });
});

test('pruning removes malformed timestamps and null records but preserves valid timestamps', () => {
  const relay = store();
  relay.data.pendingDecisions.bad = null;
  relay.data.pendingDecisions.nan = { createdAt: 'not-a-time' };
  relay.data.pendingDecisions.infinite = { createdAt: Infinity };
  relay.data.pendingDecisions.valid = { createdAt: Date.now(), installationId: 'i' };
  relay.prune();
  assert.deepEqual(Object.keys(relay.data.pendingDecisions), ['valid']);
});

test('persisted malformed batch rows do not break capacity, polling, answering, or scalar fallback', () => {
  const path = join(dir, `store-malformed-batch-${Math.random().toString(36).slice(2)}.json`);
  const seed = new RelayStore(path);
  seed.savePendingDecision({
    id: 'conduit-push-corrupt-batch', installationId: 'inst-corrupt', gatewayId: 'gw-1',
    question: 'First?', questions: [{ qid: 'q-valid', question: 'Valid question?', choices: ['Yes'] }],
  });
  seed.savePendingDecision({
    id: 'conduit-push-empty-batch', installationId: 'inst-corrupt', gatewayId: 'gw-1',
    question: 'Scalar fallback?', questions: [{ qid: 'q-valid-2', question: 'Second?', choices: ['Yes'] }],
  });
  const persisted = JSON.parse(readFileSync(path, 'utf8'));
  persisted.pendingDecisions[RelayStore.decisionKey('inst-corrupt', 'gw-1', 'conduit-push-corrupt-batch')].questions = [
    null, { qid: 'q-valid', question: 'Valid question?', choices: ['Yes'] },
  ];
  persisted.pendingDecisions[RelayStore.decisionKey('inst-corrupt', 'gw-1', 'conduit-push-empty-batch')].questions = [null, { qid: 42 }];
  writeFileSync(path, JSON.stringify(persisted));

  const relay = new RelayStore(path, { activeGlobal: 3, activePerInstallation: 3, retainedGlobal: 5, retainedPerInstallation: 5 });
  assert.equal(relay.assertPendingDecisionCapacity('another-install'), true);
  assert.deepEqual(relay.pendingDecisionStatus('inst-corrupt', 'gw-1', 'conduit-push-corrupt-batch').remaining, ['q-valid']);
  assert.equal(relay.respondPendingDecision('inst-corrupt', 'gw-1', 'conduit-push-corrupt-batch', 'Yes', 'q-valid').outcome, 'answered');
  assert.deepEqual(relay.pendingDecisionStatus('inst-corrupt', 'gw-1', 'conduit-push-corrupt-batch'), {
    status: 'answered', answers: { 'q-valid': 'Yes' }, remaining: [],
  });
  assert.equal(relay.respondPendingDecision('inst-corrupt', 'gw-1', 'conduit-push-corrupt-batch', 'Again', 'q-valid').outcome, 'already_answered');
  assert.deepEqual(relay.pendingDecisionStatus('inst-corrupt', 'gw-1', 'conduit-push-empty-batch'), { status: 'pending', deliverable: true });
  assert.equal(relay.respondPendingDecision('inst-corrupt', 'gw-1', 'conduit-push-empty-batch', 'Scalar').outcome, 'answered');
  assert.deepEqual(relay.pendingDecisionStatus('inst-corrupt', 'gw-1', 'conduit-push-empty-batch'), { status: 'answered', answer: 'Scalar' });
});

test('global active decision limit rejects new tenants without evicting a live decision', () => {
  const relay = store({ activeGlobal: 256 });
  for (let i = 0; i < 256; i += 1) {
    relay.savePendingDecision({ id: `conduit-push-${i}`, installationId: `inst-${i}`, gatewayId: 'gw-1', question: 'q' });
  }
  assert.throws(
    () => relay.savePendingDecision({ id: 'conduit-push-over', installationId: 'another-install', gatewayId: 'gw-1', question: 'q' }),
    { code: 'decision_capacity_exceeded' },
  );
  assert.equal(relay.pendingDecisionStatus('inst-0', 'gw-1', 'conduit-push-0').status, 'pending');
  assert.equal(relay.pendingDecisionStatus('inst-255', 'gw-1', 'conduit-push-255').status, 'pending');
  assert.equal(Object.keys(relay.data.pendingDecisions).length, 256);
});

test('loading an over-limit decision file preserves every unexpired record', () => {
  const path = join(dir, `store-over-limit-${Math.random().toString(36).slice(2)}.json`);
  const pendingDecisions = Object.fromEntries(Array.from({ length: 260 }, (_, i) => {
    const id = `conduit-push-${i}`;
    return [RelayStore.decisionKey(`inst-${i}`, 'gw-1', id), {
      id, installationId: `inst-${i}`, gatewayId: 'gw-1', question: 'q', choices: [],
      answers: Object.create(null), deliverable: true, createdAt: Date.now(),
    }];
  }));
  writeFileSync(path, `${JSON.stringify({ version: 1, installations: {}, pairings: {}, eventIds: {}, pendingDecisions })}\n`);
  const relay = new RelayStore(path);
  assert.equal(Object.keys(relay.data.pendingDecisions).length, 260);
  assert.equal(relay.pendingDecisionStatus('inst-0', 'gw-1', 'conduit-push-0').status, 'pending');
});

test('credential checks accept exact secrets and reject wrong or corrupt digests', () => {
  // Auth-boundary contract for the constant-time digest comparison: same
  // accept/reject behavior as before, wrong and corrupt secrets never pass,
  // and unequal-length stored digests reject without throwing.
  const relay = store();
  const { installation, deviceSecret } = relay.createInstallation({
    bundleId: 'com.milim.relay',
    deviceToken: 'a'.repeat(64),
    environment: 'production',
  });
  assert.ok(relay.authenticate(installation.id, deviceSecret, 'device'), 'correct device credential accepted');
  assert.equal(relay.authenticate(installation.id, `${deviceSecret}0`, 'device'), null, 'wrong device credential rejected');
  assert.equal(relay.authenticate(installation.id, '', 'device'), null, 'empty secret rejected');
  assert.equal(relay.authenticate('missing-installation', deviceSecret, 'device'), null);

  const pairing = relay.createPairing(installation.id);
  const claimed = relay.claimPairing(pairing.code, 'auth gateway');
  assert.ok(
    relay.authenticateGateway(installation.id, claimed.gatewayId, claimed.gatewaySecret),
    'correct gateway credential accepted',
  );
  assert.equal(
    relay.authenticateGateway(installation.id, claimed.gatewayId, `${claimed.gatewaySecret}0`),
    null,
    'wrong gateway credential rejected',
  );
  assert.equal(
    relay.authenticateGateway('some-other-installation', claimed.gatewayId, claimed.gatewaySecret),
    null,
    'cross-installation gateway credential rejected',
  );

  // Canonical stored format: hashes are 64 LOWERCASE hex characters. An
  // uppercase re-encoding of the otherwise-correct digest is noncanonical
  // stored state and must reject rather than silently broaden what the
  // store accepts.
  const deviceSecretHash = relay.data.installations[installation.id].deviceSecretHash;
  assert.ok(/^[0-9a-f]{64}$/.test(deviceSecretHash), 'hashSecret emits canonical lowercase sha256 hex');
  relay.data.installations[installation.id].deviceSecretHash = deviceSecretHash.toUpperCase();
  assert.equal(relay.authenticate(installation.id, deviceSecret, 'device'), null, 'uppercase noncanonical digest rejected');

  // A corrupt/truncated stored digest must reject cleanly — this is the
  // path that guards the timingSafeEqual unequal-length throw.
  relay.data.installations[installation.id].deviceSecretHash = 'deadbeef';
  assert.equal(relay.authenticate(installation.id, deviceSecret, 'device'), null, 'corrupt stored digest rejects without throwing');
});

// ── Dashboard identity binding (#148) ────────────────────────────────────
// A pairing may carry the opaque Conduit dashboard UUID the device intends
// it for. The binding is captured at pairing creation, becomes the gateway
// record's persistent identity at claim, and survives store reloads. The
// plugin (and anything else) can never change it per event.

test('pairing dashboard binding: created with dashboard_id, bound at claim, persisted', () => {
  const relay = store();
  const dashboardId = '0f5c8a34-1b2d-4e5f-8a9b-0c1d2e3f4a5b';
  const { installation, deviceSecret } = relay.createInstallation({
    bundleId: 'com.milim.relay',
    deviceToken: 'a'.repeat(64),
    environment: 'production',
  });

  const pairing = relay.createPairing(installation.id, dashboardId);
  const claimed = relay.claimPairing(pairing.code, 'bound gateway');
  assert.ok(claimed, 'claim succeeds');
  const gateway = relay.data.installations[installation.id].gateways[claimed.gatewayId];
  assert.equal(gateway.dashboardId, dashboardId, 'the pairing binding becomes the gateway identity');

  // The binding survives a store reload: it is durable pairing state, not
  // per-event metadata.
  const reloaded = new RelayStore(relay.path);
  const persisted = reloaded.data.installations[installation.id].gateways[claimed.gatewayId];
  assert.equal(persisted.dashboardId, dashboardId);

  // The device credential still works for re-pairing other dashboards.
  const second = relay.createPairing(installation.id, '11111111-2222-4333-8444-555555555555');
  const secondClaim = relay.claimPairing(second.code, 'other gateway');
  const secondGateway = relay.data.installations[installation.id].gateways[secondClaim.gatewayId];
  assert.equal(secondGateway.dashboardId, '11111111-2222-4333-8444-555555555555');
  assert.ok(relay.authenticate(installation.id, deviceSecret, 'device'), 'device credential unaffected');

  // One installation supports several paired gateways, each bound to its
  // own dashboard.
  assert.equal(Object.keys(relay.data.installations[installation.id].gateways).length, 2);
});

test('pairing without dashboard_id keeps the pre-dashboard gateway shape', () => {
  const relay = store();
  const { installation } = relay.createInstallation({
    bundleId: 'com.milim.relay',
    deviceToken: 'a'.repeat(64),
    environment: 'production',
  });
  const pairing = relay.createPairing(installation.id);
  const claimed = relay.claimPairing(pairing.code, 'legacy gateway');
  const gateway = relay.data.installations[installation.id].gateways[claimed.gatewayId];
  assert.equal(gateway.dashboardId, undefined, 'no binding, no dashboard identity');
});

test('createPairing validates dashboard_id at the persistence boundary', () => {
  const relay = store();
  const { installation } = relay.createInstallation({
    bundleId: 'com.milim.relay',
    deviceToken: 'a'.repeat(64),
    environment: 'production',
  });

  const persistedPairings = () => Object.values(relay.data.pairings);

  // Canonicalization: uppercase input persists the LOWERCASE canonical form.
  relay.createPairing(installation.id, '0F5C8A34-1B2D-4E5F-8A9B-0C1D2E3F4A5B');
  assert.equal(persistedPairings()[0].dashboardId, '0f5c8a34-1b2d-4e5f-8a9b-0c1d2e3f4a5b');

  // A legacy (unbound) pairing truly has NO dashboardId on the persisted
  // record — not undefined-by-convention, the key is absent.
  relay.createPairing(installation.id);
  const unbound = persistedPairings().find((pairing) => !pairing.dashboardId);
  assert.ok(unbound, 'unbound pairing persisted');
  assert.equal('dashboardId' in unbound, false, 'legacy pairing carries no dashboardId key at all');

  // Malformed and nil UUIDs are rejected, never persisted.
  const before = persistedPairings().length;
  assert.throws(() => relay.createPairing(installation.id, 'not-a-uuid'), /invalid_dashboard_id/);
  assert.throws(() => relay.createPairing(installation.id, '00000000-0000-0000-0000-000000000000'), /invalid_dashboard_id/);
  assert.throws(() => relay.createPairing(installation.id, 12345), /invalid_dashboard_id/);
  assert.equal(persistedPairings().length, before, 'rejected inputs persist nothing');
});

test('load() drops malformed persisted bindings instead of emitting them', () => {
  const relay = store();
  const { installation } = relay.createInstallation({
    bundleId: 'com.milim.relay',
    deviceToken: 'a'.repeat(64),
    environment: 'production',
  });

  // Persist a validly-bound pairing and a validly-bound gateway, then
  // corrupt BOTH bindings on disk the way a hand edit or foreign build
  // might — bypassing createPairing/claimPairing entirely.
  const pairing = relay.createPairing(installation.id, '0f5c8a34-1b2d-4e5f-8a9b-0c1d2e3f4a5b');
  const claimed = relay.claimPairing(pairing.code, 'corrupted gateway');
  const gateway = relay.data.installations[installation.id].gateways[claimed.gatewayId];
  gateway.dashboardId = 'not-a-uuid';
  for (const pairingRecord of Object.values(relay.data.pairings)) {
    pairingRecord.dashboardId = 12345;
  }
  relay.save();

  // Reload: the malformed bindings are DROPPED (fail closed), the records
  // survive unbound in the legacy shape, and valid bindings canonicalize.
  const reloaded = new RelayStore(relay.path);
  const reloadedGateway = reloaded.data.installations[installation.id].gateways[claimed.gatewayId];
  assert.equal('dashboardId' in reloadedGateway, false, 'malformed gateway binding dropped, not emitted');
  for (const pairingRecord of Object.values(reloaded.data.pairings)) {
    assert.equal('dashboardId' in pairingRecord, false, 'malformed pairing binding dropped, not emitted');
  }

  // A dropped binding can never reach a gateway: claiming the reloaded
  // (now unbound) pairing yields an unbound gateway.
  const unboundPairing = reloaded.createPairing(installation.id, '0F5C8A34-1B2D-4E5F-8A9B-0C1D2E3F4A5B');
  // Corrupt the freshly-canonicalized value too, to prove claimPairing's
  // own revalidation (defense in depth against load + claim racing a write).
  for (const pairingRecord of Object.values(reloaded.data.pairings)) {
    pairingRecord.dashboardId = 'EVIL-IDENTITY';
  }
  const secondClaim = reloaded.claimPairing(unboundPairing.code, 'post-reload gateway');
  const secondGateway = reloaded.data.installations[installation.id].gateways[secondClaim.gatewayId];
  assert.equal('dashboardId' in secondGateway, false, 'a malformed persisted pairing never becomes gateway identity');
  // notificationFor from this gateway emits no dashboard_id on the wire.
  const event = { eventId: 'response:12345678', type: 'response.ready', sessionId: 'sess-1', profile: 'default' };
  const { payload } = notificationFor(event, { show_previews: true, completion_sound: false }, secondGateway);
  assert.equal('dashboard_id' in payload.conduit, false, 'no arbitrary identity reaches the APNs payload after reload');
});

test('pre-upgrade pending decisions survive relay restart and stay answerable', () => {
  // A data file written by a pre-scoping relay: bare request-id key, no
  // `id` field, ownership only in the denormalized fields.
  const path = join(dir, `store-legacy-${Math.random().toString(36).slice(2)}.json`);
  const legacyData = {
    version: 1,
    installations: {
      'inst-legacy': { id: 'inst-legacy', active: true, gateways: {}, deviceSecretHash: 'x', preferences: {}, createdAt: '', updatedAt: '' },
    },
    pairings: {},
    eventIds: {},
    pendingDecisions: {
      'conduit-push-preupgrade': {
        installationId: 'inst-legacy',
        gatewayId: 'gw-old',
        question: 'Pre-upgrade?',
        choices: ['Red', 'Blue'],
        answers: Object.create(null),
        deliverable: true,
        createdAt: Date.now(),
      },
    },
  };
  writeFileSync(path, `${JSON.stringify(legacyData)}\n`);

  // Restart/deploy: the relay loads and re-keys the legacy decision into
  // the scoped layout using its stored ownership.
  const relay = new RelayStore(path);
  const rekeyed = relay.data.pendingDecisions[RelayStore.decisionKey('inst-legacy', 'gw-old', 'conduit-push-preupgrade')];
  assert.ok(rekeyed, 'legacy decision re-keyed into scoped layout');
  assert.equal(rekeyed.id, 'conduit-push-preupgrade');
  assert.equal('conduit-push-preupgrade' in relay.data.pendingDecisions, false, 'bare legacy key retired');
  // The repair is durable (written back atomically).
  const reloaded = new RelayStore(path);
  assert.ok(reloaded.data.pendingDecisions[RelayStore.decisionKey('inst-legacy', 'gw-old', 'conduit-push-preupgrade')]);

  // Still answerable where unambiguous: legacy resolution finds exactly
  // the one live holder, and a device answer lands on it.
  assert.deepEqual(
    relay.resolveLegacyRespond('inst-legacy', 'conduit-push-preupgrade'),
    { resolution: 'unique', gatewayId: 'gw-old' },
  );
  assert.equal(
    relay.respondPendingDecision('inst-legacy', 'gw-old', 'conduit-push-preupgrade', 'Red').outcome,
    'answered',
  );
  // The gateway poller sees the answered state (the answer loop survives).
  assert.equal(
    relay.pendingDecisionStatus('inst-legacy', 'gw-old', 'conduit-push-preupgrade').status,
    'answered',
  );
});

test('a legacy record colliding with a newer scoped decision retires, never turns ambiguous', () => {
  const path = join(dir, `store-legacy-coll-${Math.random().toString(36).slice(2)}.json`);
  const relay = new RelayStore(path);
  // Post-upgrade re-park of the same logical id (authoritative, newer).
  relay.savePendingDecision({ id: 'conduit-push-dual', installationId: 'inst-1', gatewayId: 'gw-1', question: 'New?' });
  // Simulate the pre-upgrade record still on disk alongside it.
  relay.data.pendingDecisions['conduit-push-dual'] = {
    installationId: 'inst-1',
    gatewayId: 'gw-1',
    question: 'Old?',
    choices: [],
    answers: Object.create(null),
    deliverable: true,
    createdAt: Date.now() - 1000,
  };
  relay.save();

  const reloaded = new RelayStore(path);
  const scoped = reloaded.data.pendingDecisions[RelayStore.decisionKey('inst-1', 'gw-1', 'conduit-push-dual')];
  assert.equal(scoped.question, 'New?', 'the newer scoped record is authoritative');
  assert.equal('conduit-push-dual' in reloaded.data.pendingDecisions, false, 'the legacy duplicate retired');
  // Unambiguous: exactly one live holder answers.
  assert.deepEqual(
    reloaded.resolveLegacyRespond('inst-1', 'conduit-push-dual'),
    { resolution: 'unique', gatewayId: 'gw-1' },
  );
});

test('an installation keeps a PushKit token until it is cleared', () => {
  const relay = store();
  const created = relay.createInstallation({ bundleId: 'app', deviceToken: 'a'.repeat(64), voipToken: 'b'.repeat(64), environment: 'production' });
  assert.equal(created.installation.voip, true);
  const id = created.installation.id;
  assert.equal(relay.wouldUpdateInstallation(id, { voipToken: undefined }), false, 'absent keeps it');
  assert.equal(relay.wouldUpdateInstallation(id, { voipToken: 'b'.repeat(64) }), false);
  assert.equal(relay.updateInstallation(id, { voipToken: 'c'.repeat(64) }).voip, true);
  assert.equal(relay.data.installations[id].voipToken, 'c'.repeat(64));
  assert.equal(relay.clearVoipToken(id, 'b'.repeat(64)), null, 'a token the phone replaced meanwhile stays');
  assert.equal(relay.data.installations[id].voipToken, 'c'.repeat(64));
  assert.equal(relay.clearVoipToken(id, 'c'.repeat(64)).voip, false);
  assert.equal('voipToken' in relay.data.installations[id], false);
  assert.equal(relay.data.installations[id].active, true);
  assert.equal(relay.wouldUpdateInstallation(id, { voipToken: null }), false);
  assert.equal(relay.createInstallation({ bundleId: 'app', deviceToken: 'd'.repeat(64), environment: 'production' }).installation.voip, false);
});
