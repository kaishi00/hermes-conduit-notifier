import { strict as assert } from 'node:assert';
import { randomBytes } from 'node:crypto';
import { EventEmitter } from 'node:events';
import { test } from 'node:test';

import {
  acceptKey,
  CLOSE_GRANT_CLOSED,
  CLOSE_TOO_FAST,
  closeReply,
  frameHeader,
  MAX_MESSAGE_BYTES,
  NOTICE_WATCH_CONNECTED,
  parseFrame,
  RelaySocket,
  WatchAudioBridges,
} from '../src/watch-audio.mjs';

function clientFrame(opcode, payload, { fin = true, masked = true } = {}) {
  const header = frameHeader(opcode, payload.length);
  if (!fin) header[0] &= 0x7f;
  if (!masked) return Buffer.concat([header, payload]);
  header[1] |= 0x80;
  const mask = randomBytes(4);
  const body = Buffer.from(payload.map((byte, i) => byte ^ mask[i & 3]));
  return Buffer.concat([header, mask, body]);
}

test('the accept key matches RFC 6455 section 1.3', () => {
  assert.equal(acceptKey('dGhlIHNhbXBsZSBub25jZQ=='), 's3pPLMBiTxaQ9kYGzzhZRbK+xOo=');
});

test('masked client frames of each length encoding parse back to their payload', () => {
  for (const size of [0, 1, 125, 126, 640, 65_535, 65_536]) {
    const payload = randomBytes(size);
    const frame = parseFrame(clientFrame(0x2, payload), 70_000);
    assert.equal(frame.opcode, 0x2, `size ${size}`);
    assert.deepEqual(frame.payload, payload, `size ${size}`);
    assert.equal(frame.length, clientFrame(0x2, payload).length, `size ${size}`);
  }
});

test('an incomplete frame waits for more bytes', () => {
  const frame = clientFrame(0x2, randomBytes(300));
  for (const cut of [0, 1, 2, 3, 7, frame.length - 1]) assert.equal(parseFrame(frame.subarray(0, cut)), undefined, `cut ${cut}`);
});

test('protocol violations close with the right code', () => {
  assert.deepEqual(parseFrame(clientFrame(0x2, Buffer.from('hi'), { masked: false })).error.code, 1002);
  assert.deepEqual(parseFrame(clientFrame(0x2, Buffer.from('hi'), { fin: false })).error.code, 1003);
  assert.deepEqual(parseFrame(clientFrame(0x9, Buffer.from('hi'), { fin: false })).error.code, 1002);
  assert.deepEqual(parseFrame(clientFrame(0x2, randomBytes(MAX_MESSAGE_BYTES + 1))).error.code, 1009);
  assert.deepEqual(parseFrame(clientFrame(0x9, randomBytes(126))).error.code, 1002);
  const rsv = clientFrame(0x2, Buffer.from('hi'));
  rsv[0] |= 0x40;
  assert.deepEqual(parseFrame(rsv).error.code, 1002);
});

test('two frames in one buffer parse one after the other', () => {
  const first = randomBytes(10);
  const second = randomBytes(20);
  const buffer = Buffer.concat([clientFrame(0x2, first), clientFrame(0x9, second)]);
  const a = parseFrame(buffer);
  assert.deepEqual(a.payload, first);
  const b = parseFrame(buffer.subarray(a.length));
  assert.equal(b.opcode, 0x9);
  assert.deepEqual(b.payload, second);
});

// The same masked frame with its length in a longer form than it needs.
function longFormFrame(payload, form) {
  const mask = randomBytes(4);
  const body = Buffer.from(payload.map((byte, i) => byte ^ mask[i & 3]));
  const header = form === 16 ? Buffer.alloc(4) : Buffer.alloc(10);
  header[0] = 0x82;
  if (form === 16) {
    header[1] = 0x80 | 126;
    header.writeUInt16BE(payload.length, 2);
  } else {
    header[1] = 0x80 | 127;
    header.writeUInt32BE(payload.length, 6);
  }
  return Buffer.concat([header, mask, body]);
}

test('a length in a longer form than it needs is a protocol error', () => {
  assert.equal(parseFrame(longFormFrame(randomBytes(5), 16)).error.code, 1002);
  assert.equal(parseFrame(longFormFrame(randomBytes(300), 64), 70_000).error.code, 1002);
  assert.deepEqual(parseFrame(longFormFrame(Buffer.from([7, 8]), 16).subarray(0, 3)), undefined);
});

test('a peer close is answered with a code that may go on the wire', () => {
  const close = (code) => {
    const body = Buffer.alloc(2);
    body.writeUInt16BE(code, 0);
    return body;
  };
  assert.equal(closeReply(Buffer.alloc(0)), 1000);
  assert.equal(closeReply(Buffer.from([3])), 1002);
  for (const code of [1000, 1001, 1003, 1007, 1011, 1012, 1014, 3000, 4000, 4999]) assert.equal(closeReply(close(code)), code, `code ${code}`);
  for (const code of [0, 999, 1004, 1005, 1006, 1015, 1016, 2999, 5000]) assert.equal(closeReply(close(code)), 1002, `code ${code}`);
});

// A stand-in for the upgraded net.Socket: records what the relay writes.
class FakeNetSocket extends EventEmitter {
  constructor({ full = false } = {}) {
    super();
    this.written = [];
    this.full = full;
    this.paused = false;
    this.ended = false;
  }

  setNoDelay() {}

  write(data) {
    this.written.push(Buffer.from(data));
    return !this.full;
  }

  end() {
    this.ended = true;
  }

  destroy() {
    this.ended = true;
  }

  pause() {
    this.paused = true;
  }

  resume() {
    this.paused = false;
  }

  // The close code of the relay's close frame, if it sent one.
  closeCode() {
    const frame = this.written.find((data) => (data[0] & 0x0f) === 0x8);
    return frame ? frame.readUInt16BE(2) : undefined;
  }
}

test('pings count against the rate limit, so a ping flood is cut off', () => {
  const net = new FakeNetSocket();
  const socket = new RelaySocket(net, { idleMs: 60_000 });
  const ping = clientFrame(0x9, Buffer.alloc(0));
  // 96 KB/s over 5 s is 491,520 bytes; each empty ping costs 6 + 64.
  for (let i = 0; i < 8_000 && !socket.closed; i += 1) net.emit('data', ping);
  assert.equal(net.closeCode(), CLOSE_TOO_FAST);
  socket.close();
});

test('a few pings are answered with pongs', () => {
  const net = new FakeNetSocket();
  const socket = new RelaySocket(net, { idleMs: 60_000 });
  net.emit('data', clientFrame(0x9, Buffer.from('hi')));
  assert.deepEqual(net.written, [Buffer.concat([Buffer.from([0x8a, 2]), Buffer.from('hi')])]);
  socket.close();
});

// A bridge end that records what reaches it.
function fakeEnd() {
  return {
    sent: [],
    closedWith: null,
    paused: false,
    full: false,
    onDrain: null,
    send(payload) {
      this.sent.push(payload);
      return !this.full;
    },
    close(code) {
      this.closedWith = code;
      this.onClosed?.();
    },
    pause() {
      this.paused = true;
    },
    resume() {
      this.paused = false;
    },
  };
}

function fakeGrants() {
  return { live: () => true, grants: new Map() };
}

test('a slow reader pauses the sender until it drains, and nothing is dropped', () => {
  const bridges = new WatchAudioBridges({ grants: fakeGrants(), maxBridges: 10, maxPerGateway: 2 });
  const grant = { id: 'g'.repeat(22), installationId: 'i', gatewayId: 'g' };
  const host = fakeEnd();
  const watch = fakeEnd();
  assert.equal(bridges.attachHost(grant, host), true);
  bridges.attachWatch(grant, watch);
  assert.deepEqual(host.sent, [Buffer.from([0, NOTICE_WATCH_CONNECTED])]);
  host.sent.length = 0;

  host.full = true;
  const first = Buffer.from([1, 1]);
  watch.onMessage(first);
  assert.deepEqual(host.sent, [first]);
  assert.equal(watch.paused, true);
  assert.equal(typeof host.onDrain, 'function');

  host.full = false;
  host.onDrain();
  assert.equal(watch.paused, false);
  const second = Buffer.from([1, 2]);
  watch.onMessage(second);
  assert.deepEqual(host.sent, [first, second]);
  assert.equal(watch.paused, false);
});

test('ending a bridge closes both sides without a Watch-gone notice', () => {
  const bridges = new WatchAudioBridges({ grants: fakeGrants(), maxBridges: 10, maxPerGateway: 2 });
  const grant = { id: 'h'.repeat(22), installationId: 'i', gatewayId: 'g' };
  const host = fakeEnd();
  const watch = fakeEnd();
  bridges.attachHost(grant, host);
  bridges.attachWatch(grant, watch);
  host.sent.length = 0;
  bridges.end(bridges.bridges.get(grant.id), CLOSE_GRANT_CLOSED, 'grant_closed');
  assert.equal(watch.closedWith, CLOSE_GRANT_CLOSED);
  assert.equal(host.closedWith, CLOSE_GRANT_CLOSED);
  assert.deepEqual(host.sent, []);
  assert.equal(bridges.size, 0);
});

test('a full relay says so before the upgrade, and a known bridge always takes its host back', () => {
  const bridges = new WatchAudioBridges({ grants: fakeGrants(), maxBridges: 10, maxPerGateway: 1 });
  const first = { id: 'a'.repeat(22), installationId: 'i', gatewayId: 'g' };
  const second = { id: 'b'.repeat(22), installationId: 'i', gatewayId: 'g' };
  const elsewhere = { id: 'c'.repeat(22), installationId: 'i', gatewayId: 'other' };
  assert.equal(bridges.attachHost(first, fakeEnd()), true);
  assert.equal(bridges.canAttachHost(second), false);
  assert.equal(bridges.attachHost(second, fakeEnd()), false);
  assert.equal(bridges.canAttachHost(first), true);
  assert.equal(bridges.canAttachHost(elsewhere), true);
});
