import { strict as assert } from 'node:assert';
import { test } from 'node:test';

import {
  RING_ADOPTED_MAX, RING_CONTROL_MAX_MS, RING_CONTROL_ROUTE, RING_ROUTE, RING_SESSION_MAX_CHARS, RING_SESSION_ROUTE, RING_SESSION_TTL_MS,
  RING_START_MAX_CHARS, RING_TTL_MS, Rings, ringBearer, validControl, validSessionStore, validSettle,
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
  for (const maxAdopted of [0, -1, 1.5, Number.NaN, '10']) {
    assert.throws(() => new Rings({ maxAdopted }), RangeError, String(maxAdopted));
  }
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

test('a call kept on the phone shares its mute and end through the ring, which only the Watch\'s answer opens', async () => {
  const rings = ringsAt({ now: 1_000 });
  const ring = rings.create('install-1');
  assert.equal(rings.setControl(ring.id, ring.token, 'phone', { muted: false }), 'not_answered', 'still ringing');
  assert.deepEqual(await rings.readControl(ring.id, ring.token), { status: 'not_answered' });
  rings.settle(ring.id, ring.token, 'watch', 'answered');
  assert.equal(rings.setControl(ring.id, 'x'.repeat(43), 'phone', { muted: false }), 'unknown', 'a wrong token');
  assert.deepEqual(await rings.readControl(ring.id, ring.token), { status: 'pending' }, 'nothing set yet');
  assert.deepEqual(rings.setControl(ring.id, ring.token, 'phone', { muted: false }), { control: { seq: 1, muted: false, ended: false, by: 'phone' } });
  const waiting = rings.readControl(ring.id, ring.token, { after: 1, waitMs: 5_000 });
  assert.deepEqual(rings.setControl(ring.id, ring.token, 'watch', { muted: true }), { control: { seq: 2, muted: true, ended: false, by: 'watch' } });
  assert.deepEqual(await waiting, { status: 'control', control: { seq: 2, muted: true, ended: false, by: 'watch' } });
  assert.deepEqual(await rings.readControl(ring.id, ring.token, { after: 2 }), { status: 'pending' });
  assert.deepEqual(rings.setControl(ring.id, ring.token, 'phone', { ended: true }).control, { seq: 3, muted: true, ended: true, by: 'phone' }, 'an end keeps the mute');
  assert.deepEqual(rings.setControl(ring.id, ring.token, 'watch', { muted: false }).control, { seq: 4, muted: false, ended: true, by: 'watch' }, 'and stays ended');
  assert.deepEqual(await rings.readControl(ring.id, ring.token, { after: 0 }), { status: 'control', control: { seq: 4, muted: false, ended: true, by: 'watch' } }, 'a reader that missed changes gets the whole control');

  const phoneCall = rings.create('install-1');
  rings.settle(phoneCall.id, phoneCall.token, 'phone', 'answered');
  assert.equal(rings.setControl(phoneCall.id, phoneCall.token, 'phone', { ended: true }), 'not_answered');
});

test('a control in use keeps its ring past the ring\'s time, up to its limit', async () => {
  const clock = { now: 1_000 };
  const rings = ringsAt(clock);
  const ring = rings.create('install-1');
  rings.settle(ring.id, ring.token, 'watch', 'answered');
  rings.setControl(ring.id, ring.token, 'phone', { muted: false });
  const later = rings.create('install-1');
  for (let elapsed = 0; elapsed < RING_TTL_MS * 3; elapsed += RING_TTL_MS / 2) {
    clock.now += RING_TTL_MS / 2;
    assert.equal((await rings.readControl(ring.id, ring.token)).status, 'control', 'each read keeps it');
  }
  assert.deepEqual(rings.settle(later.id, later.token, 'phone', 'answered'), {}, 'a ring with no control still goes');
  while (clock.now + RING_TTL_MS / 2 < 1_000 + RING_CONTROL_MAX_MS) {
    clock.now += RING_TTL_MS / 2;
    await rings.readControl(ring.id, ring.token);
  }
  clock.now = 1_000 + RING_CONTROL_MAX_MS - 1;
  assert.equal((await rings.readControl(ring.id, ring.token)).status, 'control', 'kept up to its limit');
  clock.now = 1_000 + RING_CONTROL_MAX_MS;
  assert.deepEqual(await rings.readControl(ring.id, ring.token), { status: 'unknown' }, 'and not a moment past it');
});

test('a full relay drops a control past its limit before a live ring', async () => {
  const clock = { now: 1_000 };
  const rings = ringsAt(clock, { maxRings: 3 });
  const answered = () => {
    const ring = rings.create('install-1');
    rings.settle(ring.id, ring.token, 'watch', 'answered');
    rings.setControl(ring.id, ring.token, 'phone', { muted: false });
    return ring;
  };
  const capped = answered();
  const latest = 1_000 + RING_CONTROL_MAX_MS - RING_TTL_MS;
  while (clock.now + RING_TTL_MS / 2 < latest) {
    clock.now += RING_TTL_MS / 2;
    await rings.readControl(capped.id, capped.token);
  }
  clock.now = latest - RING_TTL_MS / 4;
  const live = answered();
  clock.now = latest + RING_TTL_MS / 8;
  await rings.readControl(live.id, live.token);
  clock.now = latest + RING_TTL_MS / 4;
  await rings.readControl(capped.id, capped.token);
  // The capped control now sits behind the live one, and runs out first.
  clock.now = latest + RING_TTL_MS + 1;
  const ringing = rings.create('install-1');
  rings.create('install-1');
  assert.equal((await rings.readControl(live.id, live.token)).status, 'control', 'the live control stays');
  assert.ok(rings.settle(ringing.id, ringing.token, 'phone', 'answered').ring, 'and the ringing call');
  assert.deepEqual(await rings.readControl(capped.id, capped.token), { status: 'unknown' });
});

test('a control read waits a while, takes four at a time and ends when the ring goes', async () => {
  const rings = ringsAt({ now: 1_000 });
  const ring = rings.create('install-1');
  rings.settle(ring.id, ring.token, 'watch', 'answered');
  assert.deepEqual(await rings.readControl(ring.id, ring.token, { waitMs: 20 }), { status: 'pending' });
  const waits = [1, 2, 3, 4].map(() => rings.readControl(ring.id, ring.token, { waitMs: 5_000 }));
  assert.deepEqual(await rings.readControl(ring.id, ring.token, { waitMs: 5_000 }), { status: 'busy' });
  rings.forget(ring.id);
  assert.deepEqual(await Promise.all(waits), [1, 2, 3, 4].map(() => ({ status: 'pending' })));
});

test('a control write is a mute, an end or both, from the phone or the Watch', () => {
  const token = 'b'.repeat(43);
  const id = 'a'.repeat(22);
  assert.deepEqual(validControl(id, { token, by: 'watch', muted: true }), { token, by: 'watch', muted: true });
  assert.deepEqual(validControl(id, { token, by: 'phone', ended: true, muted: false }), { token, by: 'phone', muted: false, ended: true });
  assert.deepEqual(validControl(id, { token, by: 'phone', muted: true, adopt: true }), { token, by: 'phone', muted: true, adopt: true });
  assert.equal(validControl(id, { token, by: 'phone', muted: true, adopt: 'yes' }), null);
  assert.equal(validControl(id, { token, by: 'phone' }), null, 'nothing to change');
  assert.equal(validControl(id, { token, by: 'phone', ended: false }), null, 'an end is only ever set');
  assert.deepEqual(validControl(id, { token, by: 'watch', muted: true, ended: false }), { token, by: 'watch', muted: true }, 'a false end beside a mute is no end');
  assert.equal(validControl(id, { token, by: 'watch', muted: true, ended: 'no' }), null);
  assert.equal(validControl(id, { token, by: 'phone', muted: 1 }), null);
  assert.equal(validControl(id, { token, by: 'ipad', muted: true }), null);
  assert.equal(validControl(id, { token: 'short', by: 'phone', muted: true }), null);
  assert.equal(validControl('a'.repeat(21), { token, by: 'phone', muted: true }), null);
  assert.equal(RING_CONTROL_ROUTE.exec(`/v1/rings/${id}/control`)[1], id);
  assert.equal(RING_CONTROL_ROUTE.exec('/v1/rings/abc/control'), null);
});

test('after a restart a control write adopts the ring, and both devices carry on through it', async () => {
  const before = ringsAt({ now: 1_000 });
  const ring = before.create('install-1');
  before.settle(ring.id, ring.token, 'watch', 'answered');
  before.setControl(ring.id, ring.token, 'phone', { muted: true });

  const rings = ringsAt({ now: 2_000 });
  assert.deepEqual(await rings.readControl(ring.id, ring.token), { status: 'unknown' }, 'the restart dropped it');
  assert.equal(rings.setControl(ring.id, ring.token, 'watch', { muted: true }), 'unknown', 'only a write that asks adopts');
  assert.deepEqual(rings.setControl(ring.id, ring.token, 'watch', { muted: true, adopt: true }), { control: { seq: 1, muted: true, ended: false, by: 'watch' } });
  assert.deepEqual(rings.setControl(ring.id, ring.token, 'phone', { muted: true, adopt: true }).control, { seq: 2, muted: true, ended: false, by: 'phone' }, 'the other device finds it');
  assert.equal(rings.setControl(ring.id, 'x'.repeat(43), 'phone', { ended: true, adopt: true }), 'unknown', 'never with another token');
  assert.deepEqual(await rings.readControl(ring.id, ring.token), { status: 'control', control: { seq: 2, muted: true, ended: false, by: 'phone' } });
  assert.deepEqual(rings.settle(ring.id, ring.token, 'phone', 'answered'), { settled: { by: 'watch', outcome: 'answered' } }, 'an adopted ring settles nothing');
  assert.equal(rings.adopted, 1);
  assert.equal(rings.storeSession(ring.id, ring.token, 'A'.repeat(100)), 'unknown', 'an adopted ring holds no session');
  rings.forget(ring.id);
  assert.equal(rings.adopted, 0);
});

test('adopted rings are capped apart and never push out a ringing call', () => {
  const rings = ringsAt({ now: 1_000 }, { maxRings: 3, maxAdopted: 2 });
  const id = (n) => String(n).padStart(22, 'a');
  const token = 'c'.repeat(43);
  const real = rings.create('install-1');
  for (const n of [1, 2, 3]) rings.setControl(id(n), token, 'watch', { muted: false, adopt: true });
  assert.equal(rings.adopted, 2, 'the oldest adopted went');
  assert.equal(rings.setControl(id(1), token, 'watch', { muted: false }), 'unknown');
  assert.equal(rings.size, 3);
  rings.create('install-1');
  assert.equal(rings.setControl(id(4), token, 'watch', { muted: false, adopt: true }).control.seq, 1, 'room made among adopted rings');
  assert.notEqual(rings.rings.get(real.id), undefined, 'the real ring stays');
  assert.equal(RING_ADOPTED_MAX, 1_000);
});

test('a waiting read only takes a change past what it saw', async () => {
  const rings = ringsAt({ now: 1_000 });
  const ring = rings.create('install-1');
  rings.settle(ring.id, ring.token, 'watch', 'answered');
  const ahead = rings.readControl(ring.id, ring.token, { after: 5, waitMs: 5_000 });
  rings.setControl(ring.id, ring.token, 'phone', { muted: true });
  assert.deepEqual(await ahead, { status: 'pending' }, 'seq 1 is not past 5');
});
