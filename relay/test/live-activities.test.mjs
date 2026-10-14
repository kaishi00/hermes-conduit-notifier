import { strict as assert } from 'node:assert';
import { test } from 'node:test';

import {
  DISMISS_AFTER_END_S, LiveActivities, STALE_AFTER_S, liveActivityPush, validateRegistration, validateReport,
} from '../src/live-activities.mjs';

const TOKEN_A = 'a'.repeat(64);
const TOKEN_B = 'b'.repeat(64);

function harness({ maxActivities = 100, maxPerInstallation = 4, minIntervalMs = 5_000 } = {}) {
  let clock = 1_000_000;
  const sent = [];
  const results = [];
  const activities = new LiveActivities({
    maxActivities,
    maxPerInstallation,
    minIntervalMs,
    now: () => clock,
    send: async (token, push) => {
      sent.push({ token, push });
      return results.shift() ?? { ok: true, status: 200, reason: null };
    },
  });
  return { activities, sent, results, advance: (ms) => { clock += ms; } };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

test('a report reaches the activity registered under any of the chat ids', async () => {
  const { activities, sent } = harness();
  activities.register('install-1', TOKEN_A, ['runtime-1', 'stored-1']);
  assert.equal(activities.report('install-1', 'stored-1', { phase: 'tool', tool: 'terminal', tools: 2, context: 41 }), true);
  await settle();
  assert.equal(sent.length, 1);
  assert.equal(sent[0].token, TOKEN_A);
  assert.deepEqual(sent[0].push.payload.aps['content-state'], { phase: 'tool', tools: 2, tool: 'terminal', context: 41 });
  assert.equal(sent[0].push.pushType, 'liveactivity');
  assert.equal(sent[0].push.priority, 5);
});

test('nobody watching: no push, and the host hears it', async () => {
  const { activities, sent } = harness();
  activities.register('install-1', TOKEN_A, ['session-1']);
  assert.equal(activities.report('install-1', 'session-2', { phase: 'thinking', tools: 0 }), false);
  assert.equal(activities.report('install-2', 'session-1', { phase: 'thinking', tools: 0 }), false,
    'another installation never reaches this phone');
  await settle();
  assert.equal(sent.length, 0);
});

test('updates inside the interval are merged and the latest goes out when it is up', async () => {
  const { activities, sent, advance } = harness({ minIntervalMs: 30 });
  activities.register('install-1', TOKEN_A, ['session-1']);
  activities.report('install-1', 'session-1', { phase: 'thinking', tools: 0 });
  await settle();
  activities.report('install-1', 'session-1', { phase: 'tool', tool: 'web', tools: 1 });
  activities.report('install-1', 'session-1', { phase: 'tool', tool: 'files', tools: 2 });
  await settle();
  assert.equal(sent.length, 1);
  advance(30);
  await new Promise((resolve) => setTimeout(resolve, 60));
  assert.equal(sent.length, 2);
  assert.deepEqual(sent[1].push.payload.aps['content-state'], { phase: 'tool', tools: 2, tool: 'files' });
});

test('the end goes out at once, ends the activity and forgets it', async () => {
  const { activities, sent } = harness();
  activities.register('install-1', TOKEN_A, ['session-1']);
  activities.report('install-1', 'session-1', { phase: 'thinking', tools: 0 });
  await settle();
  assert.equal(activities.report('install-1', 'session-1', { phase: 'thinking', tools: 3, context: 12 }, 'done'), true);
  await settle();
  assert.equal(sent.length, 2);
  const aps = sent[1].push.payload.aps;
  assert.equal(aps.event, 'end');
  assert.equal(aps['content-state'].phase, 'done');
  assert.equal(aps['content-state'].tools, 3);
  assert.equal(aps['dismissal-date'], aps.timestamp + DISMISS_AFTER_END_S);
  assert.equal(sent[1].push.priority, 10);
  assert.equal(activities.report('install-1', 'session-1', { phase: 'thinking', tools: 0 }), false);
  assert.equal(activities.size, 0);
});

test('a token APNs no longer takes is forgotten', async () => {
  const { activities, results } = harness();
  activities.register('install-1', TOKEN_A, ['session-1']);
  results.push({ ok: false, status: 410, reason: 'Unregistered' });
  activities.report('install-1', 'session-1', { phase: 'thinking', tools: 0 });
  await settle();
  await settle();
  assert.equal(activities.size, 0);
});

test('a newer activity for the chat takes it over; old registrations make room', () => {
  const { activities } = harness({ maxPerInstallation: 2 });
  activities.register('install-1', TOKEN_A, ['session-1']);
  activities.register('install-1', TOKEN_B, ['session-1']);
  assert.equal(activities.find('install-1', 'session-1').token, TOKEN_B);
  activities.register('install-1', 'c'.repeat(64), ['session-3']);
  assert.equal(activities.size, 2, 'the oldest went past the per-phone bound');
  assert.equal(activities.find('install-1', 'session-1').token, TOKEN_B);
});

test('only the phone that registered an activity can remove it', () => {
  const { activities } = harness();
  activities.register('install-1', TOKEN_A, ['session-1']);
  assert.equal(activities.unregister('install-2', TOKEN_A), false);
  assert.equal(activities.unregister('install-1', TOKEN_A), true);
  assert.equal(activities.size, 0);
});

test('activities older than eight hours are swept', () => {
  const { activities, advance } = harness();
  activities.register('install-1', TOKEN_A, ['session-1']);
  advance(8 * 60 * 60 * 1000 + 1);
  assert.equal(activities.find('install-1', 'session-1'), undefined);
});

test('the update push is stale after a while; waiting on the user is urgent', () => {
  const update = liveActivityPush({ phase: 'approval', tools: 1 }, undefined, { nowSeconds: 100 });
  assert.equal(update.payload.aps['stale-date'], 100 + STALE_AFTER_S);
  assert.equal(update.payload.aps.event, 'update');
  assert.equal(update.priority, 10);
  assert.deepEqual(update.payload.aps['content-state'], { phase: 'approval', tools: 1 });
  const ended = liveActivityPush({ phase: 'tool', tool: 'web', tools: 1 }, 'failed', { nowSeconds: 100 });
  assert.deepEqual(ended.payload.aps['content-state'], { phase: 'failed', tools: 1, endedAt: 100 });
});

test('reports carry only known phases, categories and bounded numbers', () => {
  assert.deepEqual(validateReport({ session_id: 's-1', state: { phase: 'tool', tool: 'browser', tools: 4, context: 99 } }),
    { sessionId: 's-1', state: { phase: 'tool', tool: 'browser', tools: 4, context: 99 }, end: undefined });
  assert.deepEqual(validateReport({ session_id: 's-1', state: { phase: 'thinking' }, end: 'stopped' }),
    { sessionId: 's-1', state: { phase: 'thinking', tools: 0 }, end: 'stopped' });
  assert.equal(validateReport({ session_id: 's-1', state: { phase: 'tool', tool: 'rm -rf /' } }), null);
  assert.equal(validateReport({ session_id: 's-1', state: { phase: 'writing a novel' } }), null);
  assert.equal(validateReport({ session_id: 's-1', state: { phase: 'thinking', context: 140 } }), null);
  assert.equal(validateReport({ session_id: 's-1', state: { phase: 'thinking' }, end: 'exploded' }), null);
  assert.equal(validateReport({ session_id: 'has spaces', state: { phase: 'thinking' } }), null);
  assert.equal(validateReport({ state: { phase: 'thinking' } }), null);
  assert.equal(validateReport({ session_id: 's-1', state: { phase: 'thinking', tools: 1.5 } }), null);
});

test('registrations need an activity token and one to six chat ids', () => {
  assert.deepEqual(validateRegistration({ push_token: TOKEN_A.toUpperCase(), session_ids: ['a', 'b'] }),
    { token: TOKEN_A, sessionIds: ['a', 'b'] });
  assert.equal(validateRegistration({ push_token: 'zz', session_ids: ['a'] }), null);
  assert.equal(validateRegistration({ push_token: TOKEN_A, session_ids: [] }), null);
  assert.equal(validateRegistration({ push_token: TOKEN_A, session_ids: ['a', 'b c'] }), null);
  assert.equal(validateRegistration({ push_token: TOKEN_A, session_ids: Array.from({ length: 7 }, (_, i) => `s${i}`) }), null);
  assert.deepEqual(validateRegistration({ push_token: TOKEN_A, session_ids: Array(7).fill('a') }),
    { token: TOKEN_A, sessionIds: ['a'] }, 'repeats count once');
});

test('an end waits for the update in flight, so it never lands first', async () => {
  let release;
  const sent = [];
  const activities = new LiveActivities({
    maxActivities: 10,
    maxPerInstallation: 4,
    send: (token, push) => {
      sent.push(push.payload.aps.event);
      if (sent.length === 1) return new Promise((resolve) => { release = () => resolve({ ok: true }); });
      return { ok: true };
    },
  });
  activities.register('install-1', TOKEN_A, ['session-1']);
  activities.report('install-1', 'session-1', { phase: 'tool', tool: 'web', tools: 1 });
  await settle();
  activities.report('install-1', 'session-1', { phase: 'thinking', tools: 1 }, 'done');
  activities.report('install-1', 'session-1', { phase: 'thinking', tools: 1 });
  await settle();
  assert.deepEqual(sent, ['update']);
  release();
  await settle();
  await settle();
  assert.deepEqual(sent, ['update', 'end'], 'nothing after the end overtakes it');
  assert.equal(activities.size, 0);
});

test('an end APNs did not take is tried again before the activity is dropped', async () => {
  const { activities, sent, results } = harness({ minIntervalMs: 10 });
  activities.register('install-1', TOKEN_A, ['session-1']);
  results.push({ ok: false, status: 500, reason: 'InternalServerError' });
  activities.report('install-1', 'session-1', { phase: 'thinking', tools: 0 }, 'failed');
  await settle();
  assert.equal(sent.length, 1);
  assert.equal(activities.size, 1, 'kept until the end is delivered');
  await new Promise((resolve) => setTimeout(resolve, 30));
  await settle();
  assert.equal(sent.length, 2);
  assert.equal(sent[1].push.payload.aps.event, 'end');
  assert.equal(activities.size, 0);
});

test('a removed installation takes its activities with it', () => {
  const { activities } = harness();
  activities.register('install-1', TOKEN_A, ['session-1']);
  activities.register('install-2', TOKEN_B, ['session-1']);
  activities.removeInstallation('install-1');
  assert.equal(activities.find('install-1', 'session-1'), undefined);
  assert.ok(activities.find('install-2', 'session-1'));
});

test('a send that settles after its token was registered again leaves the new registration alone', async () => {
  let release;
  const sent = [];
  const activities = new LiveActivities({
    maxActivities: 10,
    maxPerInstallation: 4,
    minIntervalMs: 10,
    send: (token, push) => {
      sent.push(push.payload.aps.event);
      if (sent.length === 1) return new Promise((resolve) => { release = () => resolve({ ok: false, status: 500, reason: 'InternalServerError' }); });
      return { ok: true };
    },
  });
  activities.register('install-1', TOKEN_A, ['session-1']);
  activities.report('install-1', 'session-1', { phase: 'thinking', tools: 0 }, 'done');
  await settle();
  activities.register('install-1', TOKEN_A, ['session-2']);
  release();
  await settle();
  await new Promise((resolve) => setTimeout(resolve, 30));
  assert.deepEqual(sent, ['end'], 'the old end is not sent again to the new registration');
  assert.ok(activities.find('install-1', 'session-2'));
});
