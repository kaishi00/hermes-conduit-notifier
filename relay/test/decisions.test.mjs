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

test('a same-id write from another installation never clobbers a parked decision', () => {
  const relay = store();
  relay.savePendingDecision({ id: 'conduit-push-x', installationId: 'inst-1', gatewayId: 'gw-1', question: 'q' });
  relay.savePendingDecision({ id: 'conduit-push-x', installationId: 'inst-2', gatewayId: 'gw-2', question: 'evil' });
  assert.equal(relay.pendingDecisionStatus('inst-1', 'gw-1', 'conduit-push-x').status, 'pending');
  // The original installation can still answer its own decision.
  assert.equal(relay.respondPendingDecision('inst-1', 'conduit-push-x', 'Red').outcome, 'answered');
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
  relay.data.pendingDecisions['conduit-push-hidden'].deliverable = undefined;
  assert.deepEqual(
    relay.pendingDecisionStatus('inst-1', 'gw-1', 'conduit-push-hidden'),
    { status: 'pending', deliverable: true },
  );
  // Cross-installation and cross-gateway reads never see it.
  assert.deepEqual(relay.pendingDecisionStatus('inst-2', 'gw-1', 'conduit-push-abc123'), { status: 'unknown' });
  assert.deepEqual(relay.pendingDecisionStatus('inst-1', 'gw-2', 'conduit-push-abc123'), { status: 'unknown' });
  assert.deepEqual(relay.pendingDecisionStatus('inst-1', 'gw-1', 'nope'), { status: 'unknown' });

  assert.equal(relay.respondPendingDecision('inst-1', 'conduit-push-abc123', 'Red').outcome, 'answered');
  assert.deepEqual(
    relay.pendingDecisionStatus('inst-1', 'gw-1', 'conduit-push-abc123'),
    { status: 'answered', answer: 'Red' },
  );
  // A device from another installation cannot answer or re-answer.
  assert.equal(relay.respondPendingDecision('inst-2', 'conduit-push-abc123', 'Blue').outcome, 'unknown');
  assert.equal(relay.respondPendingDecision('inst-1', 'conduit-push-abc123', 'Blue').outcome, 'already_answered');
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
  const first = relay.respondPendingDecision('inst-1', 'conduit-push-batch1', 'staging', 'q0');
  assert.equal(first.outcome, 'answered');
  assert.deepEqual(first.remaining, ['q1']);
  assert.equal(relay.pendingDecisionStatus('inst-1', 'gw-1', 'conduit-push-batch1').remaining.join(','), 'q1');

  // A concurrent device locking the same qid loses (first-answer-wins per qid).
  assert.equal(relay.respondPendingDecision('inst-1', 'conduit-push-batch1', 'prod', 'q0').outcome, 'already_answered');

  // The final answer completes the batch with every locked answer.
  const last = relay.respondPendingDecision('inst-1', 'conduit-push-batch1', '["unit"]', 'q1');
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
  assert.equal(relay.respondPendingDecision('inst-1', 'conduit-push-batch2', 'a', 'q9').outcome, 'invalid_question',
    'an unknown qid on a live decision is a malformed request, not a missing decision');
  assert.equal(relay.respondPendingDecision('inst-2', 'conduit-push-batch2', 'a', 'q0').outcome, 'unknown');
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
  const result = relay.respondPendingDecision('inst-1', 'conduit-push-batch3', 'staging');
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
  assert.equal(relay.respondPendingDecision('inst-1', 'conduit-push-batch4', 'a', 'q0').outcome, 'released');
  // Cancelling an already-completed decision is reported, not an error.
  relay.savePendingDecision({
    id: 'conduit-push-batch5',
    installationId: 'inst-1',
    gatewayId: 'gw-1',
    question: 'One?',
    questions: [{ qid: 'q0', question: 'One?', choices: ['a'], multi_select: false }],
  });
  relay.respondPendingDecision('inst-1', 'conduit-push-batch5', 'a', 'q0');
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
  const stored = relay.data.pendingDecisions['conduit-push-batch6'].questions;
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
