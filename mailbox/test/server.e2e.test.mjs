import assert from 'node:assert/strict';
import { createHash, generateKeyPairSync, randomBytes, sign } from 'node:crypto';
import { once } from 'node:events';
import { request as httpRequest } from 'node:http';
import test from 'node:test';
import { setTimeout as delay } from 'node:timers/promises';
import { ReplayCache } from '../src/auth.mjs';
import { DEFAULT_LIMITS } from '../src/limits.mjs';
import { createMailbox, readConfig } from '../src/server.mjs';
import { MailboxStore } from '../src/store.mjs';

const LAIFU_TOKEN = 'laifu-send-token-0123456789';

function base64url(bytes) {
  return Buffer.from(bytes).toString('base64url');
}

function sha256(bytes) {
  return createHash('sha256').update(bytes).digest('base64url');
}

async function start(t, { limits = {}, registrationToken, proxyHops, replay } = {}) {
  const merged = { ...DEFAULT_LIMITS, ...limits };
  const store = new MailboxStore(':memory:', merged);
  const mailbox = createMailbox({ store, limits: merged, registrationToken, proxyHops, replay, log: { error() {}, info() {} } });
  mailbox.server.listen(0, '127.0.0.1');
  await once(mailbox.server, 'listening');
  t.after(() => mailbox.close());
  return { base: `http://127.0.0.1:${mailbox.server.address().port}`, mailbox };
}

// An inbox owner: an Ed25519 key that signs each request the way the
// protocol says (built by hand here, not with the server's helpers).
function owner(base) {
  const { publicKey, privateKey } = generateKeyPairSync('ed25519');
  const key = Buffer.from(publicKey.export({ format: 'jwk' }).x, 'base64url');
  const inbox = sha256(key);
  function signed(method, target, body) {
    const bytes = body === undefined ? Buffer.alloc(0) : Buffer.from(JSON.stringify(body));
    const ts = Math.floor(Date.now() / 1000);
    const nonce = randomBytes(16).toString('base64url');
    const signature = sign(null, Buffer.from(`mailbox-v1\n${method}\n${target}\n${ts}\n${nonce}\n${sha256(bytes)}`), privateKey);
    return { authorization: `Mailbox-Ed25519 ts=${ts},nonce=${nonce},sig=${base64url(signature)}`, bytes };
  }
  function call(method, target, body, { headers = {}, signal } = {}) {
    const { authorization, bytes } = signed(method, target, body);
    return fetch(base + target, { method, headers: { authorization, ...headers }, body: bytes.length ? bytes : undefined, signal });
  }
  return { key, inbox, signed, call, register: (headers) => call('POST', '/v1/inboxes', { key: base64url(key) }, { headers }) };
}

async function ownerWithSender(base) {
  const me = owner(base);
  assert.equal((await me.register()).status, 201);
  assert.equal((await me.call('PUT', `/v1/inboxes/${me.inbox}/senders/laifu`, { token_sha256: sha256(LAIFU_TOKEN) })).status, 204);
  return me;
}

function send(base, inbox, body, { token = LAIFU_TOKEN, key, headers = {} } = {}) {
  return fetch(`${base}/v1/inboxes/${inbox}/envelopes`, {
    method: 'POST',
    headers: { authorization: `Bearer ${token}`, 'content-type': 'application/octet-stream', ...(key ? { 'idempotency-key': key } : {}), ...headers },
    body,
    duplex: 'half',
  });
}

test('an owner registers, adds a sender, reads envelopes and acknowledges them', async (t) => {
  const { base } = await start(t);
  const me = owner(base);

  const registered = await me.register();
  assert.equal(registered.status, 201);
  assert.deepEqual(await registered.json(), { inbox: me.inbox, limits: { max_envelope_bytes: 262_144, ttl_seconds: 604_800, max_wait_seconds: 25 } });
  assert.equal((await me.register()).status, 200, 'registering again');
  assert.equal((await me.call('PUT', `/v1/inboxes/${me.inbox}/senders/laifu`, { token_sha256: sha256(LAIFU_TOKEN) })).status, 204);

  const sent = await send(base, me.inbox, Buffer.from('sealed-1'), { key: 'dispatch-1' });
  assert.equal(sent.status, 201);
  const { seq } = await sent.json();
  assert.equal((await send(base, me.inbox, Buffer.from('sealed-1'), { key: 'dispatch-1' })).status, 200, 'a retried send');

  const polled = await me.call('GET', `/v1/inboxes/${me.inbox}/envelopes?after=0`);
  assert.equal(polled.status, 200);
  const page = await polled.json();
  assert.equal(page.envelopes.length, 1);
  assert.deepEqual({ ...page.envelopes[0], received_at: 0 }, { seq, sender: 'laifu', received_at: 0, body: Buffer.from('sealed-1').toString('base64') });
  assert.equal(page.next, seq);

  const acked = await me.call('POST', `/v1/inboxes/${me.inbox}/ack`, { through: seq });
  assert.deepEqual(await acked.json(), { deleted: 1 });
  assert.deepEqual(await (await me.call('GET', `/v1/inboxes/${me.inbox}/envelopes?after=${seq}`)).json(), { envelopes: [], next: seq });
  assert.deepEqual(await (await me.call('GET', `/v1/inboxes/${me.inbox}`)).json(), { inbox: me.inbox, pending: 0, pending_bytes: 0, senders: 1 });
  const senders = await (await me.call('GET', `/v1/inboxes/${me.inbox}/senders`)).json();
  assert.deepEqual(senders.senders.map(({ sender, pending }) => ({ sender, pending })), [{ sender: 'laifu', pending: 0 }]);
});

test('a page holds at most `limit` envelopes and says where the next one starts', async (t) => {
  const { base } = await start(t);
  const me = await ownerWithSender(base);
  for (const text of ['a', 'b', 'c']) await send(base, me.inbox, Buffer.from(text));

  const first = await (await me.call('GET', `/v1/inboxes/${me.inbox}/envelopes?limit=2`)).json();
  assert.deepEqual(first.envelopes.map((envelope) => Buffer.from(envelope.body, 'base64').toString()), ['a', 'b']);
  const rest = await (await me.call('GET', `/v1/inboxes/${me.inbox}/envelopes?after=${first.next}`)).json();
  assert.deepEqual(rest.envelopes.map((envelope) => Buffer.from(envelope.body, 'base64').toString()), ['c']);
});

test('a waiting poll returns as soon as an envelope arrives', async (t) => {
  const { base } = await start(t);
  const me = await ownerWithSender(base);

  const started = Date.now();
  const polling = me.call('GET', `/v1/inboxes/${me.inbox}/envelopes?after=0&wait=10`);
  await delay(200);
  assert.equal((await send(base, me.inbox, Buffer.from('wake up'))).status, 201);
  const page = await (await polling).json();

  assert.deepEqual(page.envelopes.map((envelope) => Buffer.from(envelope.body, 'base64').toString()), ['wake up']);
  assert.ok(Date.now() - started < 5_000);
});

test('every waiting poll of an inbox gets the new envelope', async (t) => {
  const { base } = await start(t);
  const me = await ownerWithSender(base);

  const polls = [1, 2].map(() => me.call('GET', `/v1/inboxes/${me.inbox}/envelopes?after=0&wait=10`));
  await delay(200);
  const sentAt = Date.now();
  await send(base, me.inbox, Buffer.from('for both'));
  for (const poll of polls) {
    assert.equal((await (await poll).json()).envelopes.length, 1);
    assert.ok(Date.now() - sentAt < 5_000, 'woken, not timed out');
  }
});

test('a waiting poll ends empty when nothing arrives, and a longer wait is cut to the limit', async (t) => {
  const { base } = await start(t, { limits: { maxWaitSeconds: 1 } });
  const me = await ownerWithSender(base);

  const started = Date.now();
  const page = await (await me.call('GET', `/v1/inboxes/${me.inbox}/envelopes?wait=30`)).json();
  assert.deepEqual(page, { envelopes: [], next: 0 });
  const elapsed = Date.now() - started;
  assert.ok(elapsed >= 900 && elapsed < 5_000, `${elapsed} ms`);
});

test('a poll whose client goes away frees its place at once', async (t) => {
  const { base } = await start(t, { limits: { maxWaiters: 1 } });
  const me = await ownerWithSender(base);

  const gone = new AbortController();
  const abandoned = me.call('GET', `/v1/inboxes/${me.inbox}/envelopes?wait=10`, undefined, { signal: gone.signal }).catch(() => 'aborted');
  await delay(200);
  gone.abort();
  assert.equal(await abandoned, 'aborted');
  await delay(100);

  const waiting = me.call('GET', `/v1/inboxes/${me.inbox}/envelopes?wait=2`);
  await delay(200);
  const crowded = await me.call('GET', `/v1/inboxes/${me.inbox}/envelopes?wait=2`);
  assert.equal(crowded.status, 429);
  assert.deepEqual(await crowded.json(), { error: 'too_many_waiters' });
  assert.equal((await waiting).status, 200);
});

test('one inbox can\'t hold every waiting place', async (t) => {
  const { base } = await start(t, { limits: { maxWaitersPerInbox: 1 } });
  const me = await ownerWithSender(base);
  const neighbour = await ownerWithSender(base);

  const waiting = me.call('GET', `/v1/inboxes/${me.inbox}/envelopes?wait=2`);
  await delay(200);
  assert.equal((await me.call('GET', `/v1/inboxes/${me.inbox}/envelopes?wait=2`)).status, 429);
  assert.equal((await neighbour.call('GET', `/v1/inboxes/${neighbour.inbox}/envelopes?wait=1`)).status, 200);
  assert.equal((await waiting).status, 200);
});

test('closing the mailbox answers waiting polls so their owners poll again', async (t) => {
  const { base, mailbox } = await start(t);
  const me = await ownerWithSender(base);

  const polling = me.call('GET', `/v1/inboxes/${me.inbox}/envelopes?wait=10`);
  await delay(200);
  const started = Date.now();
  await mailbox.close();
  assert.deepEqual(await (await polling).json(), { envelopes: [], next: 0 });
  assert.ok(Date.now() - started < 2_000);
});

test('an unknown inbox, a wrong token and a foreign signature all get the same answer', async (t) => {
  const { base } = await start(t);
  const me = await ownerWithSender(base);
  const stranger = owner(base);

  for (const response of [
    await send(base, stranger.inbox, Buffer.from('x')),
    await send(base, me.inbox, Buffer.from('x'), { token: 'not-the-laifu-token-000000' }),
    await stranger.call('GET', `/v1/inboxes/${me.inbox}/envelopes`),
    await stranger.call('GET', `/v1/inboxes/${stranger.inbox}/envelopes`),
    await fetch(`${base}/v1/inboxes/${me.inbox}/envelopes`),
  ]) {
    assert.equal(response.status, 401);
    assert.deepEqual(await response.json(), { error: 'unauthorized' });
  }
});

test('a captured owner request can\'t be sent again', async (t) => {
  const { base } = await start(t);
  const me = await ownerWithSender(base);
  const target = `/v1/inboxes/${me.inbox}/ack`;
  const { authorization, bytes } = me.signed('POST', target, { through: 1 });
  const replay = () => fetch(base + target, { method: 'POST', headers: { authorization }, body: bytes });

  assert.equal((await replay()).status, 200);
  const again = await replay();
  assert.equal(again.status, 401);
  assert.deepEqual(await again.json(), { error: 'unauthorized' });
});

test('a mailbox with no room to remember another signature answers busy, not unauthorized', async (t) => {
  const { base } = await start(t, { replay: new ReplayCache(1) });
  const me = owner(base);
  assert.equal((await me.register()).status, 201);

  const busy = await me.call('GET', `/v1/inboxes/${me.inbox}`);
  assert.equal(busy.status, 503);
  assert.deepEqual(await busy.json(), { error: 'busy' });
});

test('an envelope that says it is too large is refused before it is read', async (t) => {
  const { base } = await start(t, { limits: { maxEnvelopeBytes: 16 } });
  const me = await ownerWithSender(base);

  // Says 4 MB, sends 8 bytes and waits: only an answer that doesn't read on comes back.
  const { port } = new URL(base);
  const upload = httpRequest({ port, host: '127.0.0.1', method: 'POST', path: `/v1/inboxes/${me.inbox}/envelopes`,
    headers: { authorization: `Bearer ${LAIFU_TOKEN}`, 'content-length': 4 * 1024 * 1024 } });
  upload.on('error', () => {});
  upload.write(Buffer.alloc(8));
  const [answer] = await Promise.race([once(upload, 'response'), delay(3_000).then(() => [undefined])]);
  upload.destroy();
  assert.equal(answer?.statusCode, 413);
});

test('an envelope at the size limit is accepted and one byte over is refused', async (t) => {
  const { base } = await start(t, { limits: { maxEnvelopeBytes: 16 } });
  const me = await ownerWithSender(base);

  assert.equal((await send(base, me.inbox, Buffer.alloc(16, 7))).status, 201);
  const over = await send(base, me.inbox, Buffer.alloc(17, 7));
  assert.equal(over.status, 413);
  assert.deepEqual(await over.json(), { error: 'envelope_too_large' });
  assert.equal((await send(base, me.inbox, Buffer.alloc(4 * 1024 * 1024, 7))).status, 413, 'a far larger one');
  assert.equal((await send(base, me.inbox, new Blob([Buffer.alloc(64, 7)]).stream())).status, 413, 'one sent without a length');
});

test('an operator can close registration to people without its token', async (t) => {
  const { base } = await start(t, { registrationToken: 'operator-registration-secret' });
  const me = owner(base);

  for (const headers of [{}, { 'mailbox-registration': 'operator-registration-guess' }]) {
    const refused = await me.register(headers);
    assert.equal(refused.status, 403);
    assert.deepEqual(await refused.json(), { error: 'registration_closed' });
  }
  assert.equal((await me.register({ 'mailbox-registration': 'operator-registration-secret' })).status, 201);
});

test('new inboxes are limited per client and across the mailbox', async (t) => {
  const { base } = await start(t, { proxyHops: 1, limits: { registrationsPerHourPerClient: 1, registrationsPerMinute: 2 } });
  const from = (address) => ({ 'x-forwarded-for': address });
  const first = owner(base);

  assert.equal((await first.register(from('203.0.113.1'))).status, 201);
  const again = await owner(base).register(from('203.0.113.1'));
  assert.equal(again.status, 429, 'the same client');
  assert.deepEqual(await again.json(), { error: 'rate_limited' });
  assert.equal((await first.register(from('203.0.113.1'))).status, 200, 'an inbox that exists still answers');
  assert.equal((await owner(base).register(from('203.0.113.2'))).status, 201);
  assert.equal((await owner(base).register(from('203.0.113.3'))).status, 429, 'past the mailbox-wide rate');
});

test('a sender past its rate is told to slow down', async (t) => {
  const { base } = await start(t, { limits: { sendsPerMinutePerSender: 2 } });
  const me = await ownerWithSender(base);

  assert.equal((await send(base, me.inbox, Buffer.from('1'))).status, 201);
  assert.equal((await send(base, me.inbox, Buffer.from('2'))).status, 201);
  const third = await send(base, me.inbox, Buffer.from('3'));
  assert.equal(third.status, 429);
  assert.deepEqual(await third.json(), { error: 'rate_limited' });
});

test('an owner past its rate is told to slow down, and its senders still deliver', async (t) => {
  const { base } = await start(t, { limits: { ownerRequestsPerMinutePerInbox: 3 } });
  const me = await ownerWithSender(base);

  assert.equal((await me.call('GET', `/v1/inboxes/${me.inbox}`)).status, 200);
  assert.equal((await me.call('GET', `/v1/inboxes/${me.inbox}`)).status, 200);
  const over = await me.call('GET', `/v1/inboxes/${me.inbox}`);
  assert.equal(over.status, 429);
  assert.deepEqual(await over.json(), { error: 'rate_limited' });
  assert.equal((await send(base, me.inbox, Buffer.from('still delivered'))).status, 201);
});

test('a client that keeps failing to authenticate is turned away, by the address its proxy saw', async (t) => {
  const { base } = await start(t, { proxyHops: 1, limits: { failedAuthPerMinutePerClient: 3 } });
  const me = await ownerWithSender(base);
  const from = (address) => ({ headers: { 'x-forwarded-for': `198.51.100.9, ${address}` } });

  for (const guess of ['guess-1', 'guess-2', 'guess-3']) {
    assert.equal((await send(base, me.inbox, Buffer.from('x'), { token: `${guess}-0000000000`, ...from('203.0.113.7') })).status, 401);
  }
  const blocked = await send(base, me.inbox, Buffer.from('x'), from('203.0.113.7'));
  assert.equal(blocked.status, 429);
  assert.deepEqual(await blocked.json(), { error: 'rate_limited' });
  assert.equal((await send(base, me.inbox, Buffer.from('x'), from('203.0.113.8'))).status, 201, 'another client');
});

test('removing a sender stops its token at once', async (t) => {
  const { base } = await start(t);
  const me = await ownerWithSender(base);

  assert.equal((await me.call('DELETE', `/v1/inboxes/${me.inbox}/senders/laifu`)).status, 204);
  assert.equal((await send(base, me.inbox, Buffer.from('late'))).status, 401);
  assert.equal((await me.call('DELETE', `/v1/inboxes/${me.inbox}/senders/laifu`)).status, 404);
});

test('deleting an inbox removes it with what it holds', async (t) => {
  const { base } = await start(t);
  const me = await ownerWithSender(base);
  await send(base, me.inbox, Buffer.from('pending'));

  assert.equal((await me.call('DELETE', `/v1/inboxes/${me.inbox}`)).status, 204);
  assert.equal((await me.call('GET', `/v1/inboxes/${me.inbox}/envelopes`)).status, 401);
  assert.equal((await me.register()).status, 201, 'the same key can start over');
});

test('malformed requests get 400 and unknown routes 404', async (t) => {
  const { base } = await start(t);
  const me = await ownerWithSender(base);

  assert.equal((await me.call('POST', `/v1/inboxes/${me.inbox}/ack`, {})).status, 400);
  assert.equal((await me.call('POST', `/v1/inboxes/${me.inbox}/ack`, { seqs: Array(101).fill(1) })).status, 400);
  assert.equal((await me.call('POST', `/v1/inboxes/${me.inbox}/ack`, { through: '7' })).status, 400);
  assert.equal((await me.call('PUT', `/v1/inboxes/${me.inbox}/senders/mona`, { token_sha256: 'short' })).status, 400);
  for (const query of ['after=-1', 'limit=0', 'wait=1.5', 'after=x']) {
    assert.equal((await me.call('GET', `/v1/inboxes/${me.inbox}/envelopes?${query}`)).status, 400, query);
  }
  assert.equal((await me.call('PUT', `/v1/inboxes/${me.inbox}/senders/${'x'.repeat(65)}`, { token_sha256: sha256('t') })).status, 404);
  assert.equal((await fetch(`${base}/v1/elsewhere`)).status, 404);
  assert.equal((await send(base, me.inbox, Buffer.from('x'), { key: 'has spaces' })).status, 400);
  const bad = await fetch(`${base}/v1/inboxes`, { method: 'POST', body: '{"key":"nope"}' });
  assert.equal(bad.status, 400);
  assert.deepEqual(await (await fetch(`${base}/healthz`)).json(), { ok: true });
});

test('configuration fails at boot when it is wrong', () => {
  assert.throws(() => readConfig({}), /DATA_PATH is required/);
  assert.throws(() => readConfig({ DATA_PATH: '/tmp/m.sqlite', TRUST_PROXY: 'yes' }), /TRUST_PROXY must be/);
  assert.throws(() => readConfig({ DATA_PATH: '/tmp/m.sqlite', PORT: '99999' }), /PORT must be/);
  assert.throws(() => readConfig({ DATA_PATH: '/tmp/m.sqlite', MAILBOX_REGISTRATION_TOKEN: 'short' }), /at least 16 characters/);
  const config = readConfig({ DATA_PATH: '/tmp/m.sqlite', PORT: '9200', TRUST_PROXY: '1', MAILBOX_TTL_SECONDS: '60', MAILBOX_REGISTRATION_TOKEN: '' });
  assert.equal(config.port, 9200);
  assert.equal(config.host, '127.0.0.1');
  assert.equal(config.proxyHops, 1);
  assert.equal(config.limits.ttlSeconds, 60);
  assert.equal(config.registrationToken, undefined);
  assert.equal(readConfig({ DATA_PATH: '/tmp/m.sqlite' }).port, 9130);
});
