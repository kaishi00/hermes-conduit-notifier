import { strict as assert } from 'node:assert';
import { createHash, randomBytes } from 'node:crypto';
import { EventEmitter } from 'node:events';
import { test } from 'node:test';

import { WatchToolGrants } from '../src/watch-tools.mjs';

// A stand-in for http.ServerResponse: records what was sent.
class FakeResponse extends EventEmitter {
  constructor() {
    super();
    this.headersSent = false;
    this.writableEnded = false;
    this.destroyed = false;
  }

  writeHead(status) {
    this.status = status;
    this.headersSent = true;
    return this;
  }

  end(body) {
    this.body = body ? JSON.parse(body) : undefined;
    this.writableEnded = true;
    this.emit('close');
  }
}

function grantsAt(clock, options = {}) {
  return new WatchToolGrants({ maxGrants: 8, maxPerGateway: 4, now: () => clock.now, ...options });
}

function newGrant(grants, overrides = {}) {
  const key = randomBytes(32).toString('base64url');
  const grant = grants.create({
    installationId: 'install-1',
    gatewayId: 'gateway-1',
    watchKeyHash: createHash('sha256').update(key).digest('hex'),
    ttlS: 60,
    maxCalls: 10,
    ...overrides,
  });
  return { grant, key };
}

test('a grant past its expiry is gone, and its waiting call learns so', () => {
  const clock = { now: 1_000_000 };
  const grants = grantsAt(clock, { callWaitMs: 600_000 });
  const { grant, key } = newGrant(grants);
  const waiting = new FakeResponse();
  grants.call(grant, waiting, { rid: 'r'.repeat(22), n: 'n'.repeat(16), ct: 'c'.repeat(40) });
  assert.equal(grants.authorizedWatch(grant.id, key), grant);

  clock.now += 60_000;
  assert.equal(grants.authorizedWatch(grant.id, key), undefined);
  assert.equal(waiting.status, 410);
  assert.equal(waiting.body.reason, 'expired');
  assert.equal(grants.size, 0);
});

test('the sweep closes expired grants nobody touches', () => {
  const clock = { now: 1_000_000 };
  const grants = grantsAt(clock);
  newGrant(grants, { ttlS: 60 });
  const { grant: longer } = newGrant(grants, { ttlS: 120 });
  clock.now += 61_000;
  grants.sweep();
  assert.equal(grants.size, 1);
  assert.equal(grants.live(longer.id), longer);
});

test('the relay key is compared as a hash, and malformed keys never match', () => {
  const clock = { now: 1_000_000 };
  const grants = grantsAt(clock);
  const { grant, key } = newGrant(grants);
  assert.equal(grants.authorizedWatch(grant.id, key), grant);
  assert.equal(grants.authorizedWatch(grant.id, createHash('sha256').update(key).digest('hex')), undefined);
  assert.equal(grants.authorizedWatch(grant.id, `${key}x`), undefined);
  assert.equal(grants.authorizedWatch(grant.id, undefined), undefined);
  assert.equal(grant.keyHash.length, 32);
  assert.ok(!JSON.stringify([...grants.grants.values()].map(({ waiting, seen, ...rest }) => rest)).includes(key));
});

test('the relay-wide bound refuses a new grant once full', () => {
  const clock = { now: 1_000_000 };
  const grants = grantsAt(clock, { maxGrants: 2, maxPerGateway: 4 });
  newGrant(grants, { gatewayId: 'gateway-1' });
  newGrant(grants, { gatewayId: 'gateway-2' });
  assert.throws(() => newGrant(grants, { gatewayId: 'gateway-3' }), (error) => error.status === 503 && error.message === 'watch_grant_capacity');
});

test('a newer host poll takes over from the one before', () => {
  const clock = { now: 1_000_000 };
  const grants = grantsAt(clock, { hostPollWaitMs: 600_000 });
  const { grant } = newGrant(grants);
  const first = new FakeResponse();
  const second = new FakeResponse();
  grants.poll(grant, first, 600_000);
  grants.poll(grant, second, 600_000);
  assert.deepEqual(first.body, { calls: [] });
  const call = { rid: 'r'.repeat(22), n: 'n'.repeat(16), ct: 'c'.repeat(40) };
  grants.call(grant, new FakeResponse(), call);
  assert.deepEqual(second.body, { calls: [call] });
  grants.close(grant, 'test');
});
