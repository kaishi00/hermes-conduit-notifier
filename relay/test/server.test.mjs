import { strict as assert } from 'node:assert';
import { test } from 'node:test';

import { notificationFor, validateEvent, validateDecision } from '../src/server.mjs';

const preferences = { show_previews: true, completion_sound: false };

const clarifyDecision = {
  kind: 'clarify',
  request_id: 'conduit-push-abc123',
  question: 'Which color?',
  choices: ['Red', 'Blue'],
};

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
  // Clarify is its own contract, bound to input.needed with a plugin-minted
  // request id; it may not ride an approval event.
  assert.equal(validateDecision(clarifyDecision, 'approval.needed'), undefined);
  // Kind and event type must agree.
  assert.equal(validateDecision(approvalDecision, 'input.needed'), undefined);
  assert.equal(validateDecision(approvalDecision, 'response.ready'), undefined);
  assert.equal(validateDecision({ kind: 'sudo', session_key: 's', description: 'd', choices: ['once'] }, 'approval.needed'), undefined);
});

test('validateDecision accepts the clarify contract on input events', () => {
  assert.deepEqual(
    validateDecision(clarifyDecision, 'input.needed'),
    { kind: 'clarify', request_id: 'conduit-push-abc123', question: 'Which color?', choices: ['Red', 'Blue'] },
  );
  // Answerability (request id) and display text (question) are both required.
  assert.equal(validateDecision({ kind: 'clarify', question: 'Which color?' }, 'input.needed'), undefined);
  assert.equal(validateDecision({ kind: 'clarify', request_id: 'conduit-push-abc123' }, 'input.needed'), undefined);
  // Open-ended clarifies (no choices) are valid.
  assert.deepEqual(
    validateDecision({ kind: 'clarify', request_id: 'conduit-push-abc123', question: 'What next?' }, 'input.needed'),
    { kind: 'clarify', request_id: 'conduit-push-abc123', question: 'What next?' },
  );
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

test('notificationFor embeds every batch question in both conduit payload paths', () => {
  const event = {
    type: 'input.needed',
    sessionId: 'sess-1',
    profile: 'default',
    title: 'Input needed',
    body: 'Which environment?',
    decision: {
      kind: 'clarify',
      request_id: 'conduit-push-batch-e2e',
      question: 'Which environment?',
      choices: ['staging', 'prod'],
      questions: [
        { qid: 'q0', question: 'Which environment?', choices: ['staging', 'prod'], multi_select: false },
        { qid: 'q1', question: 'Which tests?', choices: ['unit', 'ui'], multi_select: true },
      ],
    },
  };
  const preferences = { enabled: true, show_previews: true, decision_cards: true };
  const { payload } = notificationFor(event, preferences);
  // Both conduit copies (top-level and notification-center data) must carry
  // the FULL batch — this is exactly what APNs delivers to the device.
  for (const conduit of [payload.conduit, payload.body.conduit]) {
    assert.equal(conduit.decision.questions.length, 2);
    assert.deepEqual(conduit.decision.questions.map((question) => question.qid), ['q0', 'q1']);
    assert.equal(conduit.decision.questions[1].multi_select, true);
  }
});

test('notificationFor strips an oversized batch decision so the card cannot exceed APNs limits', () => {
  const event = {
    type: 'input.needed',
    sessionId: 'sess-1',
    profile: 'default',
    decision: {
      kind: 'clarify',
      request_id: 'conduit-push-huge',
      question: 'Huge?',
      questions: Array.from({ length: 8 }, (_, i) => ({
        qid: `q${i}`,
        question: 'x'.repeat(500),
        choices: Array.from({ length: 8 }, (_, j) => 'y'.repeat(80)),
        multi_select: false,
      })),
    },
  };
  const preferences = { enabled: true, show_previews: true, decision_cards: true };
  const { payload } = notificationFor(event, preferences);
  assert.equal(payload.conduit.decision, undefined, 'the size guard must strip the decision');
  assert.equal(Buffer.byteLength(JSON.stringify(payload)) <= 4096, true);
});

test('validateDecision preserves a sanitized batch and deduplicates identities', () => {
  const decision = validateDecision({
    kind: 'clarify',
    request_id: 'conduit-push-dedupe',
    question: 'summary',
    questions: [
      { qid: 'q0', question: 'First', choices: ['a', 'a', 'b'], multi_select: false },
      { qid: 'q0', question: 'Duplicate qid dropped' },
      { qid: '__proto__', question: 'Prototype qid dropped' },
      { qid: 'q1', question: 'Second', choices: [], multi_select: 'yes' },
    ],
  }, 'input.needed');
  assert.deepEqual(decision.questions.map((question) => question.qid), ['q0', 'q1']);
  assert.deepEqual(decision.questions[0].choices, ['a', 'b'], 'duplicate choice values collapse');
  assert.equal(decision.questions[1].multi_select, false, 'non-boolean multi_select never coerces to true');
});
