import { strict as assert } from 'node:assert';
import { test } from 'node:test';

import { notificationFor, validateEvent, validateDecision } from '../src/server.mjs';
import { normalizePreferences } from '../src/store.mjs';

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

test('notificationFor embeds the decision in body.conduit only; top-level stays routing-only', () => {
  const event = {
    eventId: 'approval:12345678',
    type: 'approval.needed',
    sessionId: 'sess-1',
    profile: 'default',
    decision: approvalDecision,
  };
  const { payload } = notificationFor(event, preferences);
  // body.conduit is the canonical rich payload the iOS notification path reads.
  assert.equal(payload.body.conduit.decision.kind, 'approval');
  assert.equal(payload.body.conduit.decision.session_key, 'sess-1');
  assert.equal(payload.body.conduit.decision.description, 'Run a dangerous shell command');
  assert.deepEqual(payload.body.conduit.decision.choices, ['once', 'deny']);
  // The top-level conduit copy is a ROUTING STUB: duplicating the structured
  // decision there once doubled its byte cost against the APNs size guard.
  assert.equal(payload.conduit.decision, undefined);
  assert.deepEqual(payload.conduit, {
    type: 'approval.needed',
    session_id: 'sess-1',
    profile: 'default',
    gateway: undefined,
  });
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
  assert.ok(previewsOff.payload.body.conduit.decision, 'previews-off must not disable decision cards');
  assert.equal(previewsOff.payload.aps.alert.body.includes('Run a dangerous'), false, 'banner text stays generic');
  // ...and turning just decision_cards off keeps the payload content-free.
  const cardsOff = notificationFor(event, { show_previews: true, decision_cards: false, completion_sound: false });
  assert.equal(cardsOff.payload.conduit.decision, undefined);
  assert.equal(cardsOff.payload.body.conduit.decision, undefined);
  // Legacy installations whose stored preferences predate the key default on.
  const legacy = notificationFor(event, { show_previews: false, completion_sound: false });
  assert.ok(legacy.payload.body.conduit.decision !== undefined);
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

test('clarify request IDs match the poll/respond route grammar without truncation', () => {
  const valid = { kind: 'clarify', request_id: 'Abc_123-x', question: 'What next?' };
  assert.equal(validateDecision(valid, 'input.needed').request_id, valid.request_id);
  for (const requestId of ['abc', 'abc/def', 'abc def', 'a'.repeat(129), 'é'.repeat(4)]) {
    assert.equal(validateDecision({ ...valid, request_id: requestId }, 'input.needed'), undefined, requestId);
  }
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

test('notificationFor embeds every batch question in the rich body payload', () => {
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
  // The rich body payload — exactly what APNs delivers to the device —
  // carries the FULL batch; the top-level copy stays a routing stub.
  assert.equal(payload.body.conduit.decision.questions.length, 2);
  assert.deepEqual(payload.body.conduit.decision.questions.map((question) => question.qid), ['q0', 'q1']);
  assert.equal(payload.body.conduit.decision.questions[1].multi_select, true);
  assert.equal(payload.conduit.decision, undefined);
});

test('notificationFor strips an oversized batch decision so the card cannot exceed APNs limits', () => {
  // A VALID protocol-max batch (8x8, maximal text): the answerable-card
  // capacity is bounded by the APNs payload budget, not by the 8x8
  // protocol ceiling. The degradation is ALL-OR-NOTHING — the serialized
  // push must never carry a partial questions[] (Hermes is still waiting
  // on every qid, so a truncated card would collect answers for a batch
  // that can never complete) — and the plain banner still ships.
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
  // No decision in EITHER copy, and no partial questions anywhere.
  assert.equal(payload.conduit.decision, undefined, 'the size guard must strip the decision');
  assert.equal(payload.body.conduit.decision, undefined, 'the body copy must not carry a partial card either');
  assert.equal(JSON.stringify(payload).includes('"questions"'), false, 'no partial question list may survive anywhere in the payload');
  assert.equal(JSON.stringify(payload).includes('conduit-push-huge'), false, 'no answerable card fragments may survive');
  // The plain input.needed banner still ships alongside the stripped card.
  assert.equal(payload.aps.alert.title, 'Input needed');
  assert.equal(typeof payload.aps.alert.body, 'string');
  // The stripped payload must fit under the same 3800-byte guard that
  // triggered the strip (with the guard headroom to 4096 for transport).
  assert.ok(Buffer.byteLength(JSON.stringify(payload)) <= 3800, 'stripped payload must fit under the guard threshold');
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

test('a representative multi-question batch fits the budget with a single structured copy', () => {
  // 3 questions x ~200-char text x 4 choices: comfortably deliverable now
  // that the decision is serialized once, and it carries every qid.
  const event = {
    eventId: 'input:midsize0001',
    type: 'input.needed',
    sessionId: 'sess-midsize',
    profile: 'default',
    decision: {
      kind: 'clarify',
      request_id: 'conduit-push-midsize',
      question: 'Implementation plan?',
      questions: [0, 1, 2].map((index) => ({
        qid: `q${index}`,
        question: `Describe step ${index} of the rollout, including which services are affected, the expected downtime, and how a rollback would be performed if the step fails validation.` + ' Detail '.repeat(index + 1),
        choices: [
          `Proceed with step ${index} during the maintenance window`,
          `Delay step ${index} until the follow-up release ships`,
          `Ask the platform team to review step ${index} first`,
          `Skip step ${index} entirely for this iteration`,
        ],
        multi_select: index === 2,
      })),
    },
  };
  const preferences = { enabled: true, show_previews: true, decision_cards: true };
  const { payload } = notificationFor(event, preferences);
  const size = Buffer.byteLength(JSON.stringify(payload));
  assert.equal(
    payload.body.conduit.decision.questions.length,
    3,
    'the full batch must reach the device payload',
  );
  assert.equal(payload.conduit.decision, undefined, 'top-level stays a routing stub');
  assert.ok(
    size <= 3800,
    `a representative 3-question batch must fit the guard: ${size} bytes`,
  );
});

// ── Dashboard identity in outgoing payloads (#148) ───────────────────────
// The outgoing dashboard_id comes from the AUTHENTICATED gateway record
// (bound at pairing/claim time) — never from the event body, which plugins
// must not be able to author.

test('notificationFor stamps dashboard_id from the authenticated gateway into both routing copies', () => {
  const event = { eventId: 'response:12345678', type: 'response.ready', sessionId: 'sess-1', profile: 'default' };
  const gateway = { id: 'gw-1', name: 'Mac gateway', dashboardId: '0f5c8a34-1b2d-4e5f-8a9b-0c1d2e3f4a5b' };
  const { payload } = notificationFor(event, preferences, gateway);
  assert.equal(payload.conduit.dashboard_id, gateway.dashboardId);
  assert.equal(payload.body.conduit.dashboard_id, gateway.dashboardId);
});

test('notificationFor omits dashboard_id for pre-dashboard gateways and absent gateway', () => {
  const event = { eventId: 'response:12345678', type: 'response.ready', sessionId: 'sess-1', profile: 'default' };
  const legacy = notificationFor(event, preferences, { id: 'gw-1', name: 'legacy' });
  assert.equal(legacy.payload.conduit.dashboard_id, undefined);
  assert.equal(legacy.payload.body.conduit.dashboard_id, undefined);
  const noGateway = notificationFor(event, preferences);
  assert.equal(noGateway.payload.conduit.dashboard_id, undefined);
  // Wire shape is unchanged for legacy payloads: no key at all, not null.
  assert.equal('dashboard_id' in noGateway.payload.conduit, false);
});

test('validateEvent drops a plugin-supplied dashboard_id: the event body is never a trust source', () => {
  const validated = validateEvent({
    type: 'response.ready',
    event_id: 'response:12345678',
    session_id: 'sess-1',
    dashboard_id: '99999999-9999-4999-8999-999999999999',
  });
  assert.equal(validated.dashboard_id, undefined);
  // The event object carries only the whitelisted fields.
  assert.equal('dashboard_id' in validated, false);
});

// ── Push collapse / thread isolation across dashboards (#review round 2) ─
// One installation, two gateways bound to two dashboards, both using
// profile "default" and session "default": their notifications must never
// coalesce or share a Notification Center thread, while repeated events
// from the SAME gateway/session keep existing collapse semantics and stay
// within APNs' 64-byte collapse-id cap.

const gatewayA = { id: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', name: 'A', dashboardId: '0f5c8a34-1b2d-4e5f-8a9b-0c1d2e3f4a5b' };
const gatewayB = { id: 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', name: 'B', dashboardId: '11111111-2222-4333-8444-555555555555' };

function sessionEvent(gateway, sessionId, suffix) {
  return {
    event: {
      eventId: `response:${suffix}`,
      type: 'response.ready',
      sessionId,
      profile: 'default',
    },
    gateway,
  };
}

test('identical sessions on different dashboards never coalesce or share a thread', () => {
  const a = notificationFor(sessionEvent(gatewayA, 'default', 'e1').event, preferences, sessionEvent(gatewayA, 'default', 'e1').gateway);
  const b = notificationFor(sessionEvent(gatewayB, 'default', 'e2').event, preferences, sessionEvent(gatewayB, 'default', 'e2').gateway);
  assert.notEqual(a.collapseId, b.collapseId, 'A/default and B/default must never coalesce');
  assert.notEqual(a.payload.aps['thread-id'], b.payload.aps['thread-id'], 'A/default and B/default must never share a thread');
  // Both routing copies carry the authenticated gateway's discriminator.
  assert.equal(a.payload.conduit.gateway_id, gatewayA.id);
  assert.equal(b.payload.conduit.gateway_id, gatewayB.id);
});

test('repeated events for the SAME gateway/session keep existing collapse semantics', () => {
  const first = notificationFor(sessionEvent(gatewayA, 'default', 'e1').event, preferences, sessionEvent(gatewayA, 'default', 'e1').gateway);
  const repeat = notificationFor(sessionEvent(gatewayA, 'default', 'e1').event, preferences, sessionEvent(gatewayA, 'default', 'e1').gateway);
  const sameSessionOtherType = notificationFor(
    { eventId: 'approval:1', type: 'approval.needed', sessionId: 'default', profile: 'default' },
    preferences,
    gatewayA,
  );
  assert.equal(first.collapseId, repeat.collapseId, 'identical scope collapses identically');
  assert.equal(first.payload.aps['thread-id'], repeat.payload.aps['thread-id']);
  // Cross-type same-session events share the thread but not the collapse key.
  assert.equal(first.payload.aps['thread-id'], sameSessionOtherType.payload.aps['thread-id']);
  assert.notEqual(first.collapseId, sameSessionOtherType.collapseId);
});

test('scoped collapse ids stay within the APNs 64-byte limit', () => {
  const longSession = 'x'.repeat(200);
  const { collapseId } = notificationFor(
    { eventId: 'response:1', type: 'background_task.finished', sessionId: longSession, profile: 'default' },
    preferences,
    gatewayA,
  );
  assert.ok(Buffer.byteLength(collapseId) <= 64, `collapse id must fit APNs cap, got ${Buffer.byteLength(collapseId)} bytes`);
  assert.match(collapseId, /^background_task\.finished:[0-9a-f]{16}$/);
});

test('a plugin-supplied gateway_id in the event body is dropped and can never rebind routing', () => {
  const validated = validateEvent({
    type: 'response.ready',
    event_id: 'response:gwspoof001',
    session_id: 'sess-1',
    gateway_id: 'evil-identity',
  });
  assert.equal('gateway_id' in validated, false, 'the event body is never a trust source for gateway_id');
  // The outgoing payload's discriminator comes from the AUTHENTICATED record.
  const { payload } = notificationFor(
    { eventId: 'response:gwspoof001', type: 'response.ready', sessionId: 'sess-1', profile: 'default' },
    preferences,
    gatewayA,
  );
  assert.equal(payload.conduit.gateway_id, gatewayA.id);
});

test('no-session collapse ids are gateway-scoped: identical event ids differ per gateway', () => {
  const event = { eventId: 'cron:run-42', type: 'background_task.finished' };
  const a = notificationFor(event, preferences, gatewayA);
  const b = notificationFor(event, preferences, gatewayB);
  // Same gateway + same event → deterministic (collapsible as before).
  assert.equal(a.collapseId, notificationFor(event, preferences, gatewayA).collapseId);
  // Cross-gateway same event_id → different collapse ids.
  assert.notEqual(a.collapseId, b.collapseId);
  // Bounded and token-shaped.
  assert.ok(Buffer.byteLength(a.collapseId) <= 64);
  assert.match(a.collapseId, /^background_task\.finished:[0-9a-f]{16}$/);
  // Thread differs too.
  assert.notEqual(a.payload.aps['thread-id'], b.payload.aps['thread-id']);
});

test('gateway-less direct callers keep the legacy collapse/thread shapes', () => {
  const withSession = notificationFor(
    { eventId: 'response:legacy01', type: 'response.ready', sessionId: 'sess-legacy', profile: 'default' },
    preferences,
  );
  assert.equal(withSession.collapseId, 'response.ready:sess-legacy');
  assert.equal(withSession.payload.aps['thread-id'], 'sess-legacy');
  assert.equal(withSession.threadId, 'sess-legacy');

  const noSession = notificationFor(
    { eventId: 'cron:run-42', type: 'background_task.finished' },
    preferences,
  );
  assert.equal(noSession.collapseId, 'cron:run-42');
  assert.equal(noSession.payload.aps['thread-id'], 'hermes');
  assert.equal(noSession.threadId, 'hermes');
});

test('approval and clarify pushes chime by default; attention_sound opts out; completion keeps its own toggle', () => {
  const base = { eventId: 'e:12345678', sessionId: 'sess-1', profile: 'default' };
  const sound = (type, prefs) => notificationFor({ ...base, type }, prefs).payload.aps.sound;
  // Legacy stored preferences (no attention_sound key) default to sound on.
  assert.equal(sound('approval.needed', { completion_sound: false }), 'default');
  assert.equal(sound('input.needed', { completion_sound: false }), 'default');
  assert.equal(sound('approval.needed', { attention_sound: true }), 'default');
  assert.equal(sound('approval.needed', { attention_sound: false, completion_sound: true }), undefined);
  assert.equal(sound('input.needed', { attention_sound: false, completion_sound: true }), undefined);
  // Completion sound is unchanged and independent of attention_sound.
  assert.equal(sound('response.ready', { completion_sound: true, attention_sound: false }), 'default');
  assert.equal(sound('response.ready', { completion_sound: false, attention_sound: true }), undefined);
  assert.equal(sound('turn.failed', { completion_sound: true, attention_sound: true }), undefined);
});

test('normalizePreferences defaults attention_sound on and keeps an explicit opt-out', () => {
  assert.equal(normalizePreferences({}).attention_sound, true);
  assert.equal(normalizePreferences({ attention_sound: false }).attention_sound, false);
});

test('normalizePreferences treats null as the default preference object', () => {
  assert.deepEqual(normalizePreferences(null), normalizePreferences());
});

// ── End-to-end encrypted events (#431) ─────────────────────────────────

const envelope = {
  v: 1,
  kid: '0123456789abcdef0123456789abcdef',
  msg: 'input:0123456789abcdef0123456789abcdef',
  iat: Math.floor(Date.now() / 1000),
  tok: '0123456789abcdef',
  z: 1,
  req: 'conduit-push-abc123def456',
  n: 'AAAAAAAAAAAAAAAA',
  ct: 'A'.repeat(400),
};

function encryptedEvent(overrides = {}) {
  return {
    type: 'input.needed',
    event_id: envelope.msg,
    e2e: { ...envelope },
    clarify: { request_id: envelope.req, qids: ['q0', 'q1'], card: true },
    // Plaintext content beside an envelope is never forwarded.
    title: 'Secret title',
    body: 'Secret body',
    session_id: 'sess-secret',
    profile: 'secret-profile',
    decision: clarifyDecision,
    ...overrides,
  };
}

test('validateEvent keeps an envelope and drops every plaintext content field', () => {
  const event = validateEvent(encryptedEvent());
  assert.deepEqual(event.e2e, envelope);
  assert.deepEqual(event.clarify, { request_id: envelope.req, qids: ['q0', 'q1'], card: true });
  for (const field of ['title', 'body', 'sessionId', 'profile', 'gateway', 'decision']) {
    assert.equal(event[field], undefined, field);
  }
});

test('validateEvent rejects malformed envelopes instead of falling back to plaintext', () => {
  const broken = [
    { v: 2 },
    { kid: 'ABC' },
    { msg: 'input:other0000000000' },
    { iat: 1.5 },
    { iat: Math.floor(Date.now() / 1000) - 26 * 60 * 60 },
    { iat: Math.floor(Date.now() / 1000) + 3 * 60 * 60 },
    { tok: 'zz' },
    { z: 2 },
    { req: 'x' },
    { n: 'short' },
    { ct: 'A'.repeat(2601) },
    { ct: 'not base64url!' },
  ];
  for (const patch of broken) {
    assert.throws(() => validateEvent(encryptedEvent({ e2e: { ...envelope, ...patch } })), /invalid_e2e/, JSON.stringify(patch));
  }
  // The parked request id must be the one the ciphertext is bound to.
  assert.throws(
    () => validateEvent(encryptedEvent({ clarify: { request_id: 'conduit-push-000000000000', qids: [] } })),
    /invalid_e2e/,
  );
});

test('notificationFor sends the generic alert, mutable-content and the untouched envelope', () => {
  const event = validateEvent(encryptedEvent());
  const { payload, collapseId, threadId } = notificationFor(event, { show_previews: true, attention_sound: true }, { id: 'gw-1', dashboardId: '0f5c8a34-1b2d-4e5f-8a9b-0c1d2e3f4a5b' });
  assert.deepEqual(payload.aps.alert, { title: 'Input needed', body: 'Hermes needs your response before it can continue.' });
  assert.equal(payload.aps['mutable-content'], 1);
  assert.equal(payload.aps.sound, 'default');
  assert.deepEqual(payload.conduit_e2e, envelope);
  assert.deepEqual(payload.conduit, { type: 'input.needed', e2e: 1, gateway_id: 'gw-1', dashboard_id: '0f5c8a34-1b2d-4e5f-8a9b-0c1d2e3f4a5b' });
  assert.deepEqual(payload.body.conduit, payload.conduit);
  assert.equal(collapseId, `input.needed:${threadId}`);
  assert.notEqual(threadId, envelope.tok, 'the thread token is scoped by gateway');
  for (const secret of ['Secret', 'sess-secret', 'secret-profile', 'Which color?']) {
    assert.ok(!JSON.stringify(payload).includes(secret), secret);
  }
});

test('an envelope that would overflow APNs is dropped, never truncated', () => {
  const event = validateEvent(encryptedEvent({ e2e: { ...envelope, ct: 'A'.repeat(2600) }, event_id: envelope.msg }));
  // Push the payload over the cap with a long dashboard binding stand-in.
  const { payload } = notificationFor(event, {}, { id: 'g'.repeat(1400) });
  assert.equal(payload.conduit_e2e, undefined);
  assert.equal(payload.aps['mutable-content'], undefined);
  assert.equal(payload.aps.alert.title, 'Input needed');
});

// ── Hermes calls you (#449) ──────────────────────────────────────────────

const callBody = {
  type: 'call.requested',
  event_id: 'call:0123456789abcdef01234567',
  session_id: 'st-1',
  profile: 'default',
  title: 'Hermes wants to talk',
  body: '“Check the server” finished.',
  call: { id: '0123456789abcdef01234567', kind: 'done', title: 'Check the server', session_ids: ['rt-1', 'st-1'] },
};

test('validateEvent accepts a call request and keeps its job', () => {
  const event = validateEvent(callBody);
  assert.equal(event.type, 'call.requested');
  assert.deepEqual(event.call, { id: '0123456789abcdef01234567', kind: 'done', title: 'Check the server', session_ids: ['rt-1', 'st-1'] });
});

test('validateEvent bounds the call and drops a malformed one', () => {
  const bounded = validateEvent({ ...callBody, call: { ...callBody.call, title: undefined, session_ids: ['a', 'a', 'bad id', 'b', 'c', 'd', 'e', 'x'.repeat(181)] } });
  assert.deepEqual(bounded.call, { id: callBody.call.id, kind: 'done', session_ids: ['a', 'b', 'c', 'd'] });
  for (const call of [null, [], { ...callBody.call, kind: 'maybe' }, { ...callBody.call, id: 'short' }, { ...callBody.call, id: 'has space here' }, { ...callBody.call, session_ids: [] }, { ...callBody.call, session_ids: ['bad id'] }]) {
    assert.equal(validateEvent({ ...callBody, call }).call, undefined, JSON.stringify(call));
  }
  // Only a call request carries a call.
  assert.equal(validateEvent({ ...callBody, type: 'response.ready' }).call, undefined);
});

test('a call request rings with the call category, its job and the generic copy when previews are off', () => {
  const { payload } = notificationFor(validateEvent(callBody), { show_previews: false }, { id: 'gw-1' });
  assert.equal(payload.aps.category, 'HERMES_CALL');
  assert.equal(payload.aps.sound, 'default');
  assert.equal(payload.aps.alert.title, 'Hermes wants to talk');
  assert.match(payload.aps.alert.body, /^Tap to talk to Hermes\./);
  assert.ok(!payload.aps.alert.body.includes('Check the server'));
  const { title: _title, ...untitled } = validateEvent(callBody).call;
  assert.deepEqual(payload.body.conduit.call, untitled, 'the job title stays home with previews off');
  assert.ok(!JSON.stringify(payload).includes('Check the server'));
  assert.equal(payload.conduit.call, undefined, 'the top-level copy stays routing-only');
  const silenced = notificationFor(validateEvent(callBody), { attention_sound: false }, { id: 'gw-1' });
  assert.equal(silenced.payload.aps.sound, undefined);
});

test('a call request with previews on shows the plugin copy; other pushes get no call category', () => {
  const { payload } = notificationFor(validateEvent(callBody), { show_previews: true }, { id: 'gw-1' });
  assert.deepEqual(payload.aps.alert, { title: 'Hermes wants to talk', body: '“Check the server” finished.' });
  assert.deepEqual(payload.body.conduit.call, validateEvent(callBody).call);
  const ready = notificationFor(validateEvent({ ...callBody, type: 'response.ready' }), { show_previews: true }, { id: 'gw-1' });
  assert.equal(ready.payload.aps.category, undefined);
});

test('call requests have their own preference, on by default', () => {
  assert.equal(normalizePreferences({}).call_requested, true);
  assert.equal(normalizePreferences({ call_requested: false }).call_requested, false);
});

test('an encrypted call request keeps the call category and seals its job', () => {
  const event = validateEvent({ ...callBody, event_id: envelope.msg, e2e: { ...envelope, req: '' } });
  assert.equal(event.call, undefined);
  const { payload } = notificationFor(event, {}, { id: 'gw-1' });
  assert.equal(payload.aps.category, 'HERMES_CALL');
  assert.equal(payload.aps['mutable-content'], 1);
  assert.deepEqual(payload.aps.alert, { title: 'Hermes wants to talk', body: 'Tap to talk to Hermes.' });
  assert.ok(!JSON.stringify(payload).includes('Check the server'));
});
