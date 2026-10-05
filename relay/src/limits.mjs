// Capacity defaults for the shared public relay. Every bound is a backstop
// against floods, sized far above what thousands of real installations use;
// none of them is a quota ordinary traffic should reach. Operators can tune
// each one with the RELAY_* variable next to it.

export const DEFAULT_STORE_LIMITS = Object.freeze({
  maxInstallations: 50_000,
  // Re-pairing a profile adds a gateway without revoking the old one, so a
  // long-lived installation accumulates gateways.
  maxGatewaysPerInstallation: 64,
  // Event IDs only live in memory (see event-ledger.mjs). Reaching either
  // bound forgets old IDs; it never rejects an event.
  maxEventIdsPerInstallation: 5_000,
  maxGlobalEventIds: 250_000,
  // Clarify decisions are persisted, and a full batch record can reach about
  // 13 KB, so these also cap how large a flood can make the data file.
  activeGlobal: 1_024,
  activePerInstallation: 32,
  retainedGlobal: 4_096,
  retainedPerInstallation: 128,
});

// Process-wide admissions per minute, per class of action.
export const DEFAULT_BUDGETS = Object.freeze({
  registrationsPerMinute: 24,
  eventsPerMinute: 6_000,
  // Device updates that change state, pairing creation, and valid claims.
  deviceChangesPerMinute: 240,
  // State-changing decision answers and releases.
  decisionActionsPerMinute: 600,
  // Installation deactivation and gateway revocation.
  revocationsPerMinute: 24,
});

const STORE_LIMIT_VARIABLES = Object.freeze({
  maxInstallations: 'RELAY_MAX_INSTALLATIONS',
  maxGatewaysPerInstallation: 'RELAY_MAX_GATEWAYS_PER_INSTALLATION',
  maxEventIdsPerInstallation: 'RELAY_MAX_EVENT_IDS_PER_INSTALLATION',
  maxGlobalEventIds: 'RELAY_MAX_EVENT_IDS',
  activeGlobal: 'RELAY_MAX_ACTIVE_DECISIONS',
  retainedGlobal: 'RELAY_MAX_RETAINED_DECISIONS',
});

const BUDGET_VARIABLES = Object.freeze({
  registrationsPerMinute: 'RELAY_REGISTRATIONS_PER_MINUTE',
  eventsPerMinute: 'RELAY_EVENTS_PER_MINUTE',
  deviceChangesPerMinute: 'RELAY_DEVICE_CHANGES_PER_MINUTE',
  decisionActionsPerMinute: 'RELAY_DECISION_ACTIONS_PER_MINUTE',
  revocationsPerMinute: 'RELAY_REVOCATIONS_PER_MINUTE',
});

// Unset or empty variables keep the default (Compose passes `${VAR:-}` as an
// empty string); anything else must be a positive integer, so a typo fails at
// boot instead of silently disabling a bound.
export function limitsFromEnv(env = process.env) {
  return {
    storeLimits: readIntegers(DEFAULT_STORE_LIMITS, STORE_LIMIT_VARIABLES, env),
    budgets: readIntegers(DEFAULT_BUDGETS, BUDGET_VARIABLES, env),
  };
}

function readIntegers(defaults, variables, env) {
  const values = { ...defaults };
  for (const [key, name] of Object.entries(variables)) {
    const raw = env[name];
    if (raw === undefined || raw.trim() === '') continue;
    const value = /^\d+$/.test(raw.trim()) ? Number(raw.trim()) : NaN;
    if (!Number.isSafeInteger(value) || value <= 0) throw new Error(`${name} must be a positive integer.`);
    values[key] = value;
  }
  return values;
}
