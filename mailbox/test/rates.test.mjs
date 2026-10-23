import assert from 'node:assert/strict';
import test from 'node:test';
import { RateLimits } from '../src/rates.mjs';

test('a key is allowed its maximum in each window', () => {
  const rates = new RateLimits();
  assert.equal(rates.take('owner:a', 2, 60_000, 0), true);
  assert.equal(rates.take('owner:a', 2, 60_000, 1), true);
  assert.equal(rates.take('owner:a', 2, 60_000, 2), false);
  assert.equal(rates.take('owner:b', 2, 60_000, 2), true, 'another key');
  assert.equal(rates.take('owner:a', 2, 60_000, 60_000), true, 'the next window');
});

test('exceeded says whether a key used up its window, without counting', () => {
  const rates = new RateLimits();
  assert.equal(rates.exceeded('failed:x', 2, 0), false);
  rates.take('failed:x', 2, 60_000, 0);
  for (let i = 0; i < 5; i += 1) assert.equal(rates.exceeded('failed:x', 2, 0), false);
  assert.equal(rates.take('failed:x', 2, 60_000, 0), true, 'asking didn\'t count');
  assert.equal(rates.exceeded('failed:x', 2, 59_999), true);
  assert.equal(rates.exceeded('failed:x', 2, 60_000), false, 'the next window');
});

test('a full map lets new keys through uncounted and keeps counting the keys it holds', () => {
  const rates = new RateLimits(2);
  assert.equal(rates.take('failed:a', 1, 60_000, 0), true);
  assert.equal(rates.take('failed:b', 1, 60_000, 0), true);

  for (let i = 0; i < 5; i += 1) assert.equal(rates.take('failed:c', 1, 60_000, 0), true, 'a key past the room');
  assert.equal(rates.exceeded('failed:c', 1, 0), false);
  assert.equal(rates.take('failed:a', 1, 60_000, 0), false, 'a key it holds');
});

test('a full map makes room once its windows end', () => {
  const rates = new RateLimits(2);
  rates.take('failed:a', 1, 60_000, 0);
  rates.take('failed:b', 1, 60_000, 0);

  assert.equal(rates.take('failed:c', 1, 60_000, 60_000), true);
  assert.equal(rates.take('failed:c', 1, 60_000, 60_000), false, 'counted now');
});
