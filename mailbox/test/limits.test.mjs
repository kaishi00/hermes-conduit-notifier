import assert from 'node:assert/strict';
import test from 'node:test';
import { limitsFromEnv } from '../src/limits.mjs';

test('the defaults are the spec\'s: 256 KB envelopes kept 7 days', () => {
  const limits = limitsFromEnv({});
  assert.equal(limits.maxEnvelopeBytes, 262_144);
  assert.equal(limits.ttlSeconds, 604_800);
  assert.equal(limits.maxWaitSeconds, 25);
});

test('an operator can change a limit, and an empty variable keeps the default', () => {
  assert.equal(limitsFromEnv({ MAILBOX_TTL_SECONDS: '60' }).ttlSeconds, 60);
  assert.equal(limitsFromEnv({ MAILBOX_MAX_INBOXES: ' 12 ' }).maxInboxes, 12);
  assert.equal(limitsFromEnv({ MAILBOX_TTL_SECONDS: '' }).ttlSeconds, 604_800);
});

test('a limit that is not a positive integer fails at boot', () => {
  for (const raw of ['0', '-1', '1e3', '2.5', 'many']) {
    assert.throws(() => limitsFromEnv({ MAILBOX_MAX_PENDING_PER_INBOX: raw }),
      /MAILBOX_MAX_PENDING_PER_INBOX must be a positive integer/);
  }
});
