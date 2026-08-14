import { strict as assert } from 'node:assert';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { after, test } from 'node:test';

import { RelayStore } from '../src/store.mjs';

const dir = mkdtempSync(join(tmpdir(), 'conduit-relay-decisions-'));
after(() => rmSync(dir, { recursive: true, force: true }));

function store() {
  return new RelayStore(join(dir, `store-${Math.random().toString(36).slice(2)}.json`));
}

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
    { status: 'pending' },
  );
  // Cross-installation and cross-gateway reads never see it.
  assert.deepEqual(relay.pendingDecisionStatus('inst-2', 'gw-1', 'conduit-push-abc123'), { status: 'unknown' });
  assert.deepEqual(relay.pendingDecisionStatus('inst-1', 'gw-2', 'conduit-push-abc123'), { status: 'unknown' });
  assert.deepEqual(relay.pendingDecisionStatus('inst-1', 'gw-1', 'nope'), { status: 'unknown' });

  assert.equal(relay.respondPendingDecision('inst-1', 'conduit-push-abc123', 'Red'), 'answered');
  assert.deepEqual(
    relay.pendingDecisionStatus('inst-1', 'gw-1', 'conduit-push-abc123'),
    { status: 'answered', answer: 'Red' },
  );
  // A device from another installation cannot answer or re-answer.
  assert.equal(relay.respondPendingDecision('inst-2', 'conduit-push-abc123', 'Blue'), 'unknown');
  assert.equal(relay.respondPendingDecision('inst-1', 'conduit-push-abc123', 'Blue'), 'already_answered');
  assert.equal(relay.pendingDecisionStatus('inst-1', 'gw-1', 'conduit-push-abc123').answer, 'Red');
});

test('pending decisions survive a reload and expire past the TTL', () => {
  const path = join(dir, `store-${Math.random().toString(36).slice(2)}.json`);
  const first = new RelayStore(path);
  first.savePendingDecision({ id: 'conduit-push-old', installationId: 'inst-1', gatewayId: 'gw-1', question: 'q' });

  const reloaded = new RelayStore(path);
  assert.equal(reloaded.pendingDecisionStatus('inst-1', 'gw-1', 'conduit-push-old').status, 'pending');

  // Age the record past the 2h TTL directly, then let any access prune it.
  reloaded.data.pendingDecisions['conduit-push-old'].createdAt = Date.now() - 3 * 60 * 60_000;
  assert.deepEqual(reloaded.pendingDecisionStatus('inst-1', 'gw-1', 'conduit-push-old'), { status: 'unknown' });
});

test('pending decision store is bounded', () => {
  const relay = store();
  for (let i = 0; i < 300; i += 1) {
    relay.savePendingDecision({ id: `conduit-push-${i}`, installationId: 'inst-1', gatewayId: 'gw-1', question: 'q' });
  }
  assert.ok(Object.keys(relay.data.pendingDecisions).length <= 256);
  // Oldest entries were evicted; the newest survives.
  assert.equal(relay.pendingDecisionStatus('inst-1', 'gw-1', 'conduit-push-0').status, 'unknown');
  assert.equal(relay.pendingDecisionStatus('inst-1', 'gw-1', 'conduit-push-299').status, 'pending');
});
