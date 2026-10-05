import { strict as assert } from 'node:assert';
import { test } from 'node:test';

import { EVENT_ID_TTL_MS, EventLedger } from '../src/event-ledger.mjs';

test('the periodic sweep drops expired IDs and empty installations', () => {
  const now = Date.now();
  const ledger = new EventLedger({ perInstallation: 10, total: 10 });
  ledger.add('inst-1', 'gw', 'recent', now);
  // Recorded after a newer ID, as after a clock step backwards.
  ledger.add('inst-1', 'gw', 'old', now - EVENT_ID_TTL_MS - 1);
  ledger.add('inst-2', 'gw', 'old', now - EVENT_ID_TTL_MS - 1);
  assert.equal(ledger.size, 3);

  ledger.sweep(now + 1_000);
  assert.equal(ledger.size, 3, 'sweeps run at most once a minute');
  ledger.sweep(now + 120_000);

  assert.equal(ledger.size, 1);
  assert.equal(ledger.countFor('inst-1'), 1);
  assert.equal(ledger.byInstallation.has('inst-2'), false);
  assert.equal(ledger.has('inst-1', 'gw', 'recent', now + 120_000), true);
});

test('importing persisted IDs skips malformed keys and keeps acceptance order for the bounds', () => {
  const now = Date.now();
  const ledger = new EventLedger({ perInstallation: 2, total: 10 });
  ledger.importPersisted({
    'inst:gw:newest': now,
    'inst:gw:oldest': now - 2_000,
    'inst:gw:middle': now - 1_000,
    'inst::empty-gateway': now,
    'inst:gw:': now,
    ':gw:no-installation': now,
  }, now);
  assert.equal(ledger.size, 2);
  assert.equal(ledger.has('inst', 'gw', 'oldest', now), false, 'the oldest imported ID is the one forgotten');
  assert.equal(ledger.has('inst', 'gw', 'middle', now), true);
  assert.equal(ledger.has('inst', 'gw', 'newest', now), true);
  for (const value of [null, [], 'corrupt']) ledger.importPersisted(value, now);
  assert.equal(ledger.size, 2);
});

test('an imported ID with a future time expires a day after the import', () => {
  const now = Date.now();
  const ledger = new EventLedger({ perInstallation: 10, total: 10 });
  ledger.importPersisted({ 'inst:gw:future': now + 10 * EVENT_ID_TTL_MS, 'inst:gw:infinite': Infinity }, now);
  assert.equal(ledger.size, 1, 'a non-finite time is skipped');
  assert.equal(ledger.has('inst', 'gw', 'future', now + EVENT_ID_TTL_MS), true);
  assert.equal(ledger.has('inst', 'gw', 'future', now + EVENT_ID_TTL_MS + 1), false);
});

test('a full ledger trims the largest holder in batches so it is not rescanned on every event', () => {
  const ledger = new EventLedger({ perInstallation: 2_000, total: 2_000 });
  for (let index = 0; index < 1_500; index += 1) ledger.add('heavy', 'gw', `heavy-${index}`);
  for (let index = 0; index < 500; index += 1) ledger.add('light', 'gw', `light-${index}`);
  assert.equal(ledger.size, 2_000);

  ledger.add('light', 'gw', 'light-new');

  assert.equal(ledger.size, 1_998, 'one past the bound plus 0.1% of it');
  assert.equal(ledger.countFor('heavy'), 1_497);
  assert.equal(ledger.countFor('light'), 501);
  for (const eventId of ['heavy-0', 'heavy-1', 'heavy-2']) assert.equal(ledger.has('heavy', 'gw', eventId), false);
  assert.equal(ledger.has('heavy', 'gw', 'heavy-3'), true);
  assert.equal(ledger.has('light', 'gw', 'light-0'), true);
});

test('event ID bounds must be positive integers', () => {
  for (const bound of [0, -1, 1.5, Number.NaN, undefined]) {
    assert.throws(() => new EventLedger({ perInstallation: bound, total: 10 }), /Event ID bounds must be positive integers\./);
    assert.throws(() => new EventLedger({ perInstallation: 10, total: bound }), /Event ID bounds must be positive integers\./);
  }
});
