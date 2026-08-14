import { strict as assert } from 'node:assert';
import { test } from 'node:test';

import { notificationFor, validateEvent, validateDecision } from '../src/server.mjs';

const preferences = { show_previews: true, completion_sound: false };

test('notificationFor embeds a valid decision in both conduit payload paths', () => {
  const event = {
    eventId: 'approval:12345678',
    type: 'approval.needed',
    sessionId: 'sess-1',
    profile: 'default',
    decision: { kind: 'approval', session_key: 'sess-1', description: 'Run a dangerous shell command', choices: ['once', 'deny'] },
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

test('validateEvent passes through a bounded decision', () => {
  const event = validateEvent({
    type: 'approval.needed',
    event_id: 'approval:12345678',
    session_id: 'sess-1',
    decision: { kind: 'approval', session_key: 'sess-1', description: 'd'.repeat(900), choices: ['once', 'deny', 'x'.repeat(200)] },
  });
  assert.equal(event.decision.kind, 'approval');
  assert.equal(event.decision.session_key, 'sess-1');
  assert.equal(event.decision.description.length, 500);
  assert.equal(event.decision.choices.length, 3); // all retained, each bounded
  assert.ok(event.decision.choices.every((c) => c.length <= 80));
});

test('validateDecision rejects unknown kinds and non-answerable payloads', () => {
  assert.equal(validateDecision(undefined), undefined);
  assert.equal(validateDecision({ kind: 'sudo', description: 'x', session_key: 's' }), undefined);
  assert.equal(validateDecision({ kind: 'approval', description: 'x' }), undefined); // no session_key
  assert.equal(validateDecision({ kind: 'clarify', question: 'q' }), undefined); // no request_id
  assert.equal(validateDecision({ kind: 'approval', session_key: 's', description: 'd', command: 'secret' }).command, undefined, 'unknown fields are not echoed');
});
