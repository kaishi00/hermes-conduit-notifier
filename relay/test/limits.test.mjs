import { strict as assert } from 'node:assert';
import { test } from 'node:test';

import { DEFAULT_BUDGETS, DEFAULT_STORE_LIMITS, DEFAULT_WATCH_LIMITS, limitsFromEnv } from '../src/limits.mjs';

test('relay limits default when unset or empty and accept positive integer overrides', () => {
  assert.deepEqual(limitsFromEnv({}), {
    storeLimits: { ...DEFAULT_STORE_LIMITS },
    watchLimits: { ...DEFAULT_WATCH_LIMITS },
    budgets: { ...DEFAULT_BUDGETS },
  });
  assert.deepEqual(limitsFromEnv({ RELAY_MAX_INSTALLATIONS: '', RELAY_EVENTS_PER_MINUTE: '  ' }).storeLimits, { ...DEFAULT_STORE_LIMITS },
    'Compose passes unset optional variables as empty strings');

  const configured = limitsFromEnv({
    RELAY_MAX_INSTALLATIONS: '200000',
    RELAY_MAX_GATEWAYS_PER_INSTALLATION: '128',
    RELAY_MAX_EVENT_IDS_PER_INSTALLATION: '10000',
    RELAY_MAX_EVENT_IDS: '1000000',
    RELAY_MAX_ACTIVE_DECISIONS: '4096',
    RELAY_MAX_RETAINED_DECISIONS: ' 16384 ',
    RELAY_REGISTRATIONS_PER_MINUTE: '60',
    RELAY_EVENTS_PER_MINUTE: '12000',
    RELAY_DEVICE_CHANGES_PER_MINUTE: '480',
    RELAY_DECISION_ACTIONS_PER_MINUTE: '1200',
    RELAY_REVOCATIONS_PER_MINUTE: '48',
    RELAY_MAX_WATCH_GRANTS: '4096',
    RELAY_MAX_WATCH_GRANTS_PER_GATEWAY: '8',
    RELAY_WATCH_CALLS_PER_MINUTE: '2400',
    RELAY_MAX_WATCH_AUDIO_BRIDGES: '50',
    RELAY_MAX_WATCH_AUDIO_BRIDGES_PER_GATEWAY: '1',
  });
  assert.deepEqual(configured.storeLimits, {
    ...DEFAULT_STORE_LIMITS,
    maxInstallations: 200_000,
    maxGatewaysPerInstallation: 128,
    maxEventIdsPerInstallation: 10_000,
    maxGlobalEventIds: 1_000_000,
    activeGlobal: 4_096,
    retainedGlobal: 16_384,
  });
  assert.deepEqual(configured.budgets, {
    registrationsPerMinute: 60,
    eventsPerMinute: 12_000,
    deviceChangesPerMinute: 480,
    decisionActionsPerMinute: 1_200,
    revocationsPerMinute: 48,
    watchCallsPerMinute: 2_400,
  });
  assert.deepEqual(configured.watchLimits, {
    maxGrants: 4_096,
    maxGrantsPerGateway: 8,
    maxAudioBridges: 50,
    maxAudioBridgesPerGateway: 1,
  });
});

test('an invalid relay limit names its variable instead of disabling the bound', () => {
  for (const value of ['0', '-1', '1.5', '10k', 'Infinity', '9007199254740993']) {
    assert.throws(() => limitsFromEnv({ RELAY_MAX_EVENT_IDS: value }), /^Error: RELAY_MAX_EVENT_IDS must be a positive integer\.$/, value);
  }
});
