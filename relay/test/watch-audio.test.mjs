import { strict as assert } from 'node:assert';
import { randomBytes } from 'node:crypto';
import { test } from 'node:test';

import { acceptKey, frameHeader, MAX_MESSAGE_BYTES, parseFrame } from '../src/watch-audio.mjs';

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
