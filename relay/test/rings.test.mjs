import { strict as assert } from 'node:assert';
import { test } from 'node:test';

import {
  RING_ROUTE, RING_SESSION_MAX_CHARS, RING_SESSION_ROUTE, RING_SESSION_TTL_MS, RING_START_MAX_CHARS, RING_TTL_MS,
  Rings, ringBearer, validSessionStore, validSettle,
} from '../src/rings.mjs';

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
  assert.equal(RING_ROUTE.exec(`/v1/rings/${'a'.repeat(22)}/settled`)[1], 'a'.repeat(22));
  assert.equal(RING_ROUTE.exec('/v1/rings/abc/settled'), null, 'the route takes only a ring id');
  assert.equal(RING_ROUTE.exec(`/v1/rings/${'a'.repeat(22)}`), null);
});

test('a ring no push carried is forgotten', () => {
  const rings = ringsAt({ now: 1_000 });
  const { id, token } = rings.create('install-1');
  rings.forget(id);
  assert.equal(rings.size, 0);
  assert.deepEqual(rings.settle(id, token, 'phone', 'answered'), {});
});

test('the ring maximum is a positive whole number', () => {
  for (const maxRings of [0, -1, 1.5, Number.NaN, '10']) {
    assert.throws(() => new Rings({ maxRings }), RangeError, String(maxRings));
  }
  assert.equal(new Rings({ maxRings: 1 }).maxRings, 1);
});

test('a Watch answer keeps its sealed start for the phone; nothing else does', () => {
  const rings = ringsAt({ now: 1_000 });
  const answered = rings.create('install-1');
  assert.equal(rings.settle(answered.id, answered.token, 'watch', 'answered', 'sealed-start').ring.start, 'sealed-start');
  const declined = rings.create('install-1');
  assert.equal(rings.settle(declined.id, declined.token, 'watch', 'declined', 'sealed-start').ring.start, undefined);
  const phone = rings.create('install-1');
  assert.equal(rings.settle(phone.id, phone.token, 'phone', 'answered', 'sealed-start').ring.start, undefined);
});

test('a settle may carry a sealed start; a malformed one is dropped, not refused', () => {
  const token = 'b'.repeat(43);
  const id = 'a'.repeat(22);
  assert.deepEqual(validSettle(id, { token, by: 'watch', outcome: 'answered', start: 'Zm9v_-' }), { token, by: 'watch', outcome: 'answered', start: 'Zm9v_-' });
  for (const start of ['', 'not base64url!', 'x'.repeat(RING_START_MAX_CHARS + 1), 7, { a: 1 }]) {
    assert.deepEqual(validSettle(id, { token, by: 'watch', outcome: 'answered', start }), { token, by: 'watch', outcome: 'answered' }, String(start).slice(0, 20));
  }
});

test('the phone stores a session only for a ring the Watch answered, and the Watch takes it', async () => {
  const rings = ringsAt({ now: 1_000 });
  const ring = rings.create('install-1');
  assert.equal(rings.storeSession(ring.id, ring.token, 'sealed'), 'not_answered', 'still ringing');
  assert.deepEqual(await rings.takeSession(ring.id, ring.token), { status: 'not_answered' });
  rings.settle(ring.id, ring.token, 'watch', 'answered');
  assert.deepEqual(await rings.takeSession(ring.id, ring.token), { status: 'pending' }, 'nothing stored yet, and no wait');
  assert.equal(rings.storeSession(ring.id, 'x'.repeat(43), 'sealed'), 'unknown', 'a wrong token');
  assert.equal(rings.storeSession(ring.id, ring.token, 'sealed-1'), 'stored');
  assert.equal(rings.storeSession(ring.id, ring.token, 'sealed-22'), 'stored', 'a second store replaces the first');
  assert.equal(rings.sessionChars, 'sealed-22'.length);
  assert.deepEqual(await rings.takeSession(ring.id, ring.token), { status: 'session', sealed: 'sealed-22' });
  assert.deepEqual(await rings.takeSession(ring.id, ring.token), { status: 'session', sealed: 'sealed-22' }, 'a fetch whose answer was lost asks again');
  assert.deepEqual(await rings.takeSession(ring.id, 'x'.repeat(43)), { status: 'unknown' });

  const declined = rings.create('install-1');
  rings.settle(declined.id, declined.token, 'phone', 'answered');
  assert.equal(rings.storeSession(declined.id, declined.token, 'sealed'), 'not_answered', 'the phone has this call');
});

test('a waiting fetch gets the session as soon as it is stored', async () => {
  const rings = ringsAt({ now: 1_000 });
  const ring = rings.create('install-1');
  rings.settle(ring.id, ring.token, 'watch', 'answered');
  const waiting = rings.takeSession(ring.id, ring.token, { waitMs: 5_000 });
  rings.storeSession(ring.id, ring.token, 'sealed');
  assert.deepEqual(await waiting, { status: 'session', sealed: 'sealed' });
  assert.equal(rings.rings.get(ring.id).waiters.size, 0);
});

test('a wait ends empty after its time, when its request goes away, or when the ring goes', async () => {
  const rings = ringsAt({ now: 1_000 });
  const ring = rings.create('install-1');
  rings.settle(ring.id, ring.token, 'watch', 'answered');
  assert.deepEqual(await rings.takeSession(ring.id, ring.token, { waitMs: 20 }), { status: 'pending' });
  const gone = new AbortController();
  const aborted = rings.takeSession(ring.id, ring.token, { waitMs: 5_000, signal: gone.signal });
  gone.abort();
  assert.deepEqual(await aborted, { status: 'pending' });
  const dropped = rings.takeSession(ring.id, ring.token, { waitMs: 5_000 });
  rings.forget(ring.id);
  assert.deepEqual(await dropped, { status: 'pending' });
  assert.deepEqual(await rings.takeSession(ring.id, ring.token), { status: 'unknown' });
});

test('a ring takes two waiting fetches at a time', async () => {
  const rings = ringsAt({ now: 1_000 });
  const ring = rings.create('install-1');
  rings.settle(ring.id, ring.token, 'watch', 'answered');
  const first = rings.takeSession(ring.id, ring.token, { waitMs: 5_000 });
  const second = rings.takeSession(ring.id, ring.token, { waitMs: 5_000 });
  assert.deepEqual(await rings.takeSession(ring.id, ring.token, { waitMs: 5_000 }), { status: 'busy' });
  rings.storeSession(ring.id, ring.token, 'sealed');
  assert.deepEqual(await Promise.all([first, second]), [{ status: 'session', sealed: 'sealed' }, { status: 'session', sealed: 'sealed' }]);
});

test('a session lasts two minutes, and the oldest go first at the size limit', async () => {
  const clock = { now: 1_000 };
  const rings = ringsAt(clock, { maxSessionChars: 10 });
  const answered = () => {
    const ring = rings.create('install-1');
    rings.settle(ring.id, ring.token, 'watch', 'answered');
    return ring;
  };
  const old = answered();
  const newer = answered();
  rings.storeSession(old.id, old.token, 'aaaaaa');
  rings.storeSession(newer.id, newer.token, 'bbbbbb');
  assert.equal(rings.sessionChars, 6, 'the older session made room');
  assert.deepEqual(await rings.takeSession(old.id, old.token), { status: 'pending' });
  assert.deepEqual(await rings.takeSession(newer.id, newer.token), { status: 'session', sealed: 'bbbbbb' });
  clock.now += RING_SESSION_TTL_MS;
  assert.deepEqual(await rings.takeSession(newer.id, newer.token), { status: 'pending' }, 'too old to start a call');
  assert.equal(rings.sessionChars, 0);
  clock.now += RING_TTL_MS;
  rings.sweep();
  assert.equal(rings.size, 0);
});

test('a session store and fetch are well formed', () => {
  const token = 'b'.repeat(43);
  const id = 'a'.repeat(22);
  assert.deepEqual(validSessionStore(id, { token, sealed: 'Zm9v' }), { token, sealed: 'Zm9v' });
  assert.equal(validSessionStore(id, { token, sealed: 'x'.repeat(RING_SESSION_MAX_CHARS + 1) }), null);
  assert.equal(validSessionStore(id, { token, sealed: '' }), null);
  assert.equal(validSessionStore(id, { token, sealed: 'a b' }), null);
  assert.equal(validSessionStore(id, { token: 'short', sealed: 'Zm9v' }), null);
  assert.equal(validSessionStore('short', { token, sealed: 'Zm9v' }), null);
  assert.equal(ringBearer(`Bearer ${token}`), token);
  assert.equal(ringBearer(`Bearer ${token}x`), null);
  assert.equal(ringBearer(undefined), null);
  assert.equal(RING_SESSION_ROUTE.exec(`/v1/rings/${id}/session`)[1], id);
  assert.equal(RING_SESSION_ROUTE.exec(`/v1/rings/${id}/settled`), null);
  assert.equal(RING_ROUTE.exec(`/v1/rings/${id}/session`), null);
});
