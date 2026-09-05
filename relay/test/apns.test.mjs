import { strict as assert } from 'node:assert';
import { generateKeyPairSync } from 'node:crypto';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { createServer as createNetServer } from 'node:net';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createServer as createHttp2Server } from 'node:http2';
import { after, before, test } from 'node:test';

import { ApnsClient } from '../src/apns.mjs';

// Transport-level tests for the REAL ApnsClient — no APNS_MODE stub, no
// mocking of send(). The APNS_MODE seams bypass ApnsClient entirely, so a
// connection-level ClientHttp2Session failure (the shape that used to crash
// the whole relay as an unhandled 'error' event) was never exercised. Here
// the client talks to actual endpoints: a released loopback port (real
// connection refusal → session 'error') and a real local HTTP/2 server
// (success and APNs-style rejection responses).

const dir = mkdtempSync(join(tmpdir(), 'conduit-apns-'));
const keyPath = join(dir, 'ephemeral-key.pem');
const { privateKey } = generateKeyPairSync('ec', { namedCurve: 'P-256' });
writeFileSync(keyPath, privateKey.export({ type: 'sec1', format: 'pem' }));

after(() => rmSync(dir, { recursive: true, force: true }));

function client(origin) {
  return new ApnsClient({ keyPath, keyId: 'AAAAAAAAAA', teamId: 'BBBBBBBBBB', topic: 'com.milim.relay', origin });
}

const notification = { payload: { aps: { alert: { title: 't', body: 'b' } } } };

// Reserve a loopback port and release it: connecting there must be refused,
// which surfaces as a genuine session-level 'error' inside http2.connect.
function closedPort() {
  return new Promise((resolve, reject) => {
    const probe = createNetServer();
    probe.listen(0, '127.0.0.1', () => {
      const port = probe.address().port;
      probe.close(() => resolve(port));
    });
    probe.on('error', reject);
  });
}

function startH2Server(handler) {
  return new Promise((resolve, reject) => {
    const server = createHttp2Server();
    server.on('stream', handler);
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => resolve({ server, origin: `http://127.0.0.1:${server.address().port}` }));
  });
}

test('a real connection-level session error rejects send() exactly once and the process survives', async () => {
  const { server, origin } = await startH2Server((stream) => {
    // Accept the stream, then tear the SERVER session down ungracefully
    // while the request is in flight (destroying the h2 session, NOT the
    // raw socket — Node forbids socket manipulation through a live session).
    // The client sees BOTH a stream error and a session error for this one
    // send, which is exactly the double-settlement shape the settle-once
    // guard exists for.
    stream.session.destroy();
  });
  const apns = client(origin);
  try {
    await assert.rejects(
      apns.send('a'.repeat(64), notification),
      (error) => {
        // The rejected promise is the ONLY failure surface the server sees;
        // assert it is a real error, not a hang or a synthetic value.
        assert.ok(error instanceof Error);
        return true;
      },
    );
    // Reaching this line at all is the regression proof: before the session
    // 'error' handler existed, an unhandled 'error' event terminated the
    // entire Node process. A second send proves the client and process are
    // still healthy and settle-once did not wedge the promise machinery.
    await assert.rejects(apns.send('b'.repeat(64), notification), Error);
  } finally {
    server.close();
    server.closeAllConnections?.();
  }
});

test('a refused connection surfaces as a rejected send() without crashing the process', async () => {
  const port = await closedPort();
  const apns = client(`https://127.0.0.1:${port}`);
  await assert.rejects(apns.send('c'.repeat(64), notification), (error) => {
    assert.ok(error instanceof Error);
    return true;
  });
  // Still alive: another send against another dead port fails the same way.
  const port2 = await closedPort();
  await assert.rejects(client(`https://127.0.0.1:${port2}`).send('d'.repeat(64), notification), Error);
});

test('a real 200 APNs response resolves exactly once with ok:true', async () => {
  const { server, origin } = await startH2Server((stream) => {
    stream.respond({ ':status': 200, 'content-type': 'application/json' });
    stream.end('{}');
  });
  try {
    const result = await client(origin).send('e'.repeat(64), notification);
    assert.deepEqual(result, { ok: true, status: 200, reason: null });
  } finally {
    server.close();
    server.closeAllConnections?.();
  }
});

test('a real APNs rejection response resolves with ok:false and the reason', async () => {
  const { server, origin } = await startH2Server((stream) => {
    stream.respond({ ':status': 403, 'content-type': 'application/json' });
    stream.end(JSON.stringify({ reason: 'BadDeviceToken' }));
  });
  try {
    const result = await client(origin).send('f'.repeat(64), notification);
    assert.deepEqual(result, { ok: false, status: 403, reason: 'BadDeviceToken' });
  } finally {
    server.close();
    server.closeAllConnections?.();
  }
});

// Guard the default origin so the DI seam cannot silently change production.
test('the default origin is production APNs', () => {
  const apns = new ApnsClient({ keyPath, keyId: 'AAAAAAAAAA', teamId: 'BBBBBBBBBB', topic: 'com.milim.relay' });
  assert.equal(apns.origin, 'https://api.push.apple.com');
});
