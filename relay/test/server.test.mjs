import { strict as assert } from 'node:assert';
import { test } from 'node:test';

import { notificationFor, validateEvent, validateDecision } from '../src/server.mjs';

const preferences = { show_previews: true, completion_sound: false };

const approvalDecision = {
  kind: 'approval',
  session_key: 'sess-1',
  description: 'Run a dangerous shell command',
  choices: ['once', 'deny'],
};

test('notificationFor embeds a valid decision in both conduit payload paths', () => {
  const event = {
    eventId: 'approval:12345678',
    type: 'approval.needed',
    sessionId: 'sess-1',
    profile: 'default',
    decision: approvalDecision,
  };
  const { payload } = notificationFor(event, preferences);
  assert.equal(payload.conduit.decision.kind, 'approval');
  assert.equal(payload.conduit.decision.session_key, 'sess-1');
  assert.equal(payload.conduit.decision.description, 'Run a dangerous shell command');
  assert.deepEqual(payload.conduit.decision.choices, ['once', 'deny']);
  // The expo-notifications `body` mirror must carry the same decision.
  assert.deepEqual(payload.body.conduit, payload.conduit);
});

test('notificationFor omits decision when the event has none', () => {
  const event = { eventId: 'response:12345678', type: 'response.ready', sessionId: 'sess-1', profile: 'default' };
  const { payload } = notificationFor(event, preferences);
  assert.equal(payload.conduit.decision, undefined);
  assert.equal(payload.body.conduit.decision, undefined);
});

test('notificationFor drops a malformed decision so the notification degrades to a routing stub', () => {
  const event = {
    eventId: 'approval:12345678',
    type: 'approval.needed',
    sessionId: 'sess-1',
    profile: 'default',
    decision: { kind: 'approval', description: 'no session key' }, // not answerable
  };
  const { payload } = notificationFor(event, preferences);
  assert.equal(payload.conduit.decision, undefined);
});

test('notificationFor gates decision on the dedicated decision_cards preference', () => {
  const event = {
    eventId: 'approval:12345678',
    type: 'approval.needed',
    sessionId: 'sess-1',
    profile: 'default',
    decision: approvalDecision,
  };
  // Decision cards are independent of show_previews: previews-off users who
  // left decision_cards on still get answerable cards...
  const previewsOff = notificationFor(event, { show_previews: false, completion_sound: false });
  assert.ok(previewsOff.payload.conduit.decision, 'previews-off must not disable decision cards');
  assert.equal(previewsOff.payload.aps.alert.body.includes('Run a dangerous'), false, 'banner text stays generic');
  // ...and turning just decision_cards off keeps the payload content-free.
  const cardsOff = notificationFor(event, { show_previews: true, decision_cards: false, completion_sound: false });
  assert.equal(cardsOff.payload.conduit.decision, undefined);
  assert.equal(cardsOff.payload.body.conduit.decision, undefined);
  // Legacy installations whose stored preferences predate the key default on.
  const legacy = notificationFor(event, { show_previews: false, completion_sound: false });
  assert.ok(legacy.payload.conduit.decision !== undefined);
});

test('notificationFor degrades to a routing stub when the payload would exceed the APNs cap', () => {
  const event = {
    eventId: 'approval:12345678',
    type: 'approval.needed',
    sessionId: 'sess-1',
    profile: 'default',
    title: 'Approval needed',
    // 500 multi-byte chars in the alert body plus a 500-char multi-byte
    // description (echoed in both conduit copies) push the encoded payload
    // past the 4 KB APNs cap.
    body: '€'.repeat(500),
    decision: { ...approvalDecision, description: '€'.repeat(500) },
  };
  const { payload } = notificationFor(event, preferences);
  assert.equal(payload.conduit.decision, undefined, 'oversized decision must be dropped');
  assert.equal(payload.conduit.session_id, 'sess-1', 'routing stub must survive');
  assert.ok(Buffer.byteLength(JSON.stringify(payload)) <= 3800);
});

test('validateDecision only accepts approval decisions on approval events', () => {
  assert.equal(validateDecision(approvalDecision, 'approval.needed').kind, 'approval');
  // Not shipped yet: clarify cannot be answered from the payload.
  assert.equal(validateDecision({ kind: 'clarify', request_id: 'r', question: 'q' }, 'input.needed'), undefined);
  // Kind and event type must agree.
  assert.equal(validateDecision(approvalDecision, 'input.needed'), undefined);
  assert.equal(validateDecision(approvalDecision, 'response.ready'), undefined);
  assert.equal(validateDecision({ kind: 'sudo', session_key: 's', description: 'd', choices: ['once'] }, 'approval.needed'), undefined);
});

test('validateDecision whitelists choices to the approval vocabulary', () => {
  const injected = validateDecision(
    { ...approvalDecision, choices: ['once', 'deny', 'not-a-real-choice', 'free text'] },
    'approval.needed',
  );
  assert.deepEqual(injected.choices, ['once', 'deny']);
  // All-invalid choices degrade the whole decision to a routing stub.
  assert.equal(
    validateDecision({ ...approvalDecision, choices: ['bogus'] }, 'approval.needed'),
    undefined,
  );
  // Missing or empty choices likewise.
  assert.equal(validateDecision({ kind: 'approval', session_key: 's', description: 'd' }, 'approval.needed'), undefined);
});

test('validateDecision rejects missing display text or session key', () => {
  assert.equal(validateDecision({ kind: 'approval', session_key: 's', choices: ['once'] }, 'approval.needed'), undefined);
  assert.equal(validateDecision({ kind: 'approval', description: 'd', choices: ['once'] }, 'approval.needed'), undefined);
  assert.equal(validateDecision(undefined, 'approval.needed'), undefined);
  // Unknown fields are never echoed into the payload.
  const clean = validateDecision({ ...approvalDecision, command: 'secret' }, 'approval.needed');
  assert.equal(clean.command, undefined);
});

test('validateEvent passes a bounded decision through', () => {
  const event = validateEvent({
    type: 'approval.needed',
    event_id: 'approval:12345678',
    session_id: 'sess-1',
    decision: { kind: 'approval', session_key: 'sess-1', description: 'd'.repeat(900), choices: ['once', 'deny', 'x'.repeat(200)] },
  });
  assert.equal(event.decision.kind, 'approval');
  assert.equal(event.decision.session_key, 'sess-1');
  assert.equal(event.decision.description.length, 500);
  assert.deepEqual(event.decision.choices, ['once', 'deny'], 'unknown choice strings are filtered');
});
