import { strict as assert } from 'node:assert';
import { test } from 'node:test';

import { RING_ROUTE, RING_TTL_MS, Rings, validSettle } from '../src/rings.mjs';

function ringsAt(clock, options = {}) {
  return new Rings({ now: () => clock.now, ...options });
}

test('the first settle wins and later ones learn how it was settled', () => {
  const clock = { now: 1_000 };
  const rings = ringsAt(clock);
  const { id, token } = rings.create('install-1');
  assert.match(id, /^[A-Za-z0-9_-]{22}$/);
  assert.match(token, /^[A-Za-z0-9_-]{43}$/);
  const first = rings.settle(id, token, 'watch', 'answered');
  assert.equal(first.ring.installationId, 'install-1');
  assert.deepEqual(first.ring.settled, { by: 'watch', outcome: 'answered' });
  assert.deepEqual(rings.settle(id, token, 'phone', 'declined'), { settled: { by: 'watch', outcome: 'answered' } });
});

test('a wrong token and an unknown ring look alike', () => {
  const rings = ringsAt({ now: 1_000 });
  const { id } = rings.create('install-1');
  const other = rings.create('install-2');
  assert.deepEqual(rings.settle(id, other.token, 'phone', 'answered'), {});
  assert.deepEqual(rings.settle('A'.repeat(22), other.token, 'phone', 'answered'), {});
  assert.equal(rings.settle(id, other.token, 'phone', 'answered').ring, undefined, 'a wrong token settles nothing');
});

test('a ring is forgotten after its time and the oldest goes first at the maximum', () => {
  const clock = { now: 1_000 };
  const rings = ringsAt(clock, { maxRings: 2 });
  const old = rings.create('install-1');
  clock.now += 1;
  const middle = rings.create('install-1');
  rings.create('install-1');
  assert.equal(rings.size, 2);
  assert.deepEqual(rings.settle(old.id, old.token, 'phone', 'answered'), {}, 'evicted');
  clock.now += RING_TTL_MS;
  assert.deepEqual(rings.settle(middle.id, middle.token, 'phone', 'answered'), {}, 'expired');
  assert.equal(rings.size, 0);
});

test('a settle names the ring, who settled it and how', () => {
  const token = 'b'.repeat(43);
  assert.deepEqual(validSettle('a'.repeat(22), { token, by: 'phone', outcome: 'answered' }), { token, by: 'phone', outcome: 'answered' });
  assert.deepEqual(validSettle('a'.repeat(22), { token, by: 'watch', outcome: 'declined', extra: 1 }), { token, by: 'watch', outcome: 'declined' });
  assert.equal(validSettle('a'.repeat(21), { token, by: 'phone', outcome: 'answered' }), null);
  assert.equal(validSettle('a'.repeat(22), { token: 'short', by: 'phone', outcome: 'answered' }), null);
  assert.equal(validSettle('a'.repeat(22), { token, by: 'ipad', outcome: 'answered' }), null);
  assert.equal(validSettle('a'.repeat(22), { token, by: 'phone', outcome: 'missed' }), null);
  assert.equal(validSettle('a'.repeat(22), null), null);
  assert.equal(RING_ROUTE.exec('/v1/rings/abc/settled')[1], 'abc');
  assert.equal(RING_ROUTE.exec('/v1/rings/abc'), null);
});
