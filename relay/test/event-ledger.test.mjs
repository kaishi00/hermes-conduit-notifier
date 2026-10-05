import { strict as assert } from 'node:assert';
import { test } from 'node:test';

import { EVENT_ID_TTL_MS, EventLedger } from '../src/event-ledger.mjs';

test('the periodic sweep drops expired IDs and empty installations', () => {
  const now = Date.now();
  const ledger = new EventLedger({ perInstallation: 10, total: 10 });
  ledger.add('inst-1', 'gw', 'old', now - EVENT_ID_TTL_MS - 1);
  ledger.add('inst-1', 'gw', 'recent', now);
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
