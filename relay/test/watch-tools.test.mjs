import { strict as assert } from 'node:assert';
import { createHash, randomBytes } from 'node:crypto';
import { EventEmitter } from 'node:events';
import { test } from 'node:test';

import { WatchToolGrants, watchToolRoutes } from '../src/watch-tools.mjs';

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

test('a host poll that drops does not count as seeing the host; one that completes does', async (t) => {
  const clock = { now: 1_000_000 };
  const grants = grantsAt(clock, { hostPollWaitMs: 600_000, hostGoneMs: 40_000, callWaitMs: 600_000 });
  const { grant } = newGrant(grants);
  // Its long timers end with the grant, whatever the assertions do.
  t.after(() => grants.close(grant, 'test'));
  const call = () => ({ rid: randomBytes(16).toString('base64url'), n: 'n'.repeat(16), ct: 'c'.repeat(40) });

  // The host's connection drops 30 s into its poll (it died).
  const dropped = new FakeResponse();
  grants.poll(grant, dropped, 600_000);
  clock.now += 30_000;
  dropped.destroyed = true;
  dropped.emit('close');
  assert.equal(grant.hostWaiter, null);
  // 45 s after that poll began: past HOST_GONE_MS, so calls fail at once.
  clock.now += 15_000;
  const refused = new FakeResponse();
  grants.call(grant, refused, call());
  assert.equal(refused.status, 503);
  assert.equal(refused.body.error, 'host_offline');

  // A poll that runs its course is the host being there.
  const completed = new FakeResponse();
  grants.poll(grant, completed, 0);
  clock.now += 30_000;
  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.deepEqual(completed.body, { calls: [] });
  clock.now += 15_000;
  const queued = new FakeResponse();
  grants.call(grant, queued, call());
  assert.equal(queued.status, undefined, 'waiting for the host, not refused');
  grants.close(grant, 'test');
  assert.equal(queued.status, 410);
});

function routesFor(grants, { warnings = [] } = {}) {
  const route = watchToolRoutes({
    grants,
    readJson: async (request) => request.body,
    enforceRateLimit: () => {},
    enforceCallBudget: () => {},
    authenticateGateway: (request) => request.gateway,
    clientAddress: () => '203.0.113.1',
    warn: (message, fields) => warnings.push({ message, ...fields }),
  });
  return async (method, path, { gateway, bearer, body } = {}) => {
    const response = new FakeResponse();
    const request = { method, gateway, body, headers: bearer ? { authorization: `Bearer ${bearer}` } : {} };
    assert.equal(await route(request, response, new URL(path, 'https://relay.example')), true);
    return response;
  };
}

const grantBody = () => ({ watch_key_sha256: createHash('sha256').update(randomBytes(32).toString('base64url')).digest('hex'), ttl_s: 600, max_calls: 60 });

test('a full relay answers watch_grant_capacity and warns, rather than failing as a relay error', async () => {
  const clock = { now: 1_000_000 };
  const grants = grantsAt(clock, { maxGrants: 1 });
  const warnings = [];
  const request = routesFor(grants, { warnings });
  const first = await request('POST', '/v1/watch-tools/grants', { gateway: { installationId: 'i', gatewayId: 'g1' }, body: grantBody() });
  assert.equal(first.status, 201);
  const full = await request('POST', '/v1/watch-tools/grants', { gateway: { installationId: 'i', gatewayId: 'g2' }, body: grantBody() });
  assert.equal(full.status, 503);
  assert.deepEqual(full.body, { error: 'watch_grant_capacity' });
  assert.deepEqual(warnings, [{ message: 'watch tool grants at capacity', grants: 1 }]);
});

test('a host poll without wait_ms, or with an empty one, waits the default', async (t) => {
  const clock = { now: 1_000_000 };
  const grants = grantsAt(clock, { hostPollWaitMs: 600_000 });
  const gateway = { installationId: 'install-1', gatewayId: 'gateway-1' };
  const { grant } = newGrant(grants);
  t.after(() => grants.close(grant, 'test'));
  const request = routesFor(grants);
  for (const query of ['', '?wait_ms=', '?wait_ms=%20']) {
    const polled = await request('GET', `/v1/watch-tools/grants/${grant.id}/calls${query}`, { gateway });
    await new Promise((resolve) => setTimeout(resolve, 5));
    assert.equal(polled.status, undefined, `still held: "${query}"`);
    assert.equal(grant.hostWaiter?.response, polled);
  }
  const now = await request('GET', `/v1/watch-tools/grants/${grant.id}/calls?wait_ms=0`, { gateway });
  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.deepEqual(now.body, { calls: [] });
});

test('every DELETE answers 204: a wrong key or another gateway closes nothing and learns nothing', async () => {
  const clock = { now: 1_000_000 };
  const grants = grantsAt(clock);
  const { grant, key } = newGrant(grants);
  const request = routesFor(grants);
  const path = `/v1/watch-tools/grants/${grant.id}`;
  const wrongKey = await request('DELETE', path, { bearer: randomBytes(32).toString('base64url') });
  const unknown = await request('DELETE', `/v1/watch-tools/grants/${randomBytes(16).toString('base64url')}`, { bearer: key });
  const otherGateway = await request('DELETE', path, { gateway: { installationId: 'install-1', gatewayId: 'gateway-2' } });
  assert.deepEqual([wrongKey.status, unknown.status, otherGateway.status], [204, 204, 204]);
  assert.equal(grants.live(grant.id), grant);
  assert.equal((await request('DELETE', path, { bearer: key })).status, 204);
  assert.equal(grants.live(grant.id), undefined);
});
