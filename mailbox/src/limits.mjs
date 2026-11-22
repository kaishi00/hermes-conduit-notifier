// Capacity defaults for the rooms mailbox. Envelope size and lifetime are
// the protocol's (256 KB, 7 days); the rest are backstops against floods,
// sized far above what linked Hermes installations use. Operators can tune
// each one with the MAILBOX_* variable next to it.

export const DEFAULT_LIMITS = Object.freeze({
  maxEnvelopeBytes: 262_144,
  ttlSeconds: 604_800,
  // Longest a poll waits for an envelope, under common proxy timeouts (60 s).
  maxWaitSeconds: 25,
  // Far more than the registration rate can make before an untouched inbox
  // is deleted as idle (6 a minute for 90 days is 777,600), so with open
  // registration nobody can fill it just by registering: they would have to
  // keep hundreds of thousands of inboxes in use for months (notifier#1014).
  // An inbox row is about 200 bytes, so a full mailbox holds about 200 MB of them.
  maxInboxes: 1_000_000,
  // One sender per link the inbox's owner made.
  maxSendersPerInbox: 64,
  maxPendingPerInbox: 2_000,
  maxPendingBytesPerInbox: 67_108_864,
  // So one link can't fill its peer's inbox for everyone else.
  maxPendingPerSender: 500,
  maxTotalBytes: 4_294_967_296,
  // A RoomLink turn is dozens of small envelopes each way.
  sendsPerMinutePerSender: 600,
  ownerRequestsPerMinutePerInbox: 600,
  // New inboxes, mailbox-wide; one network (an IPv4 /24 or an IPv6 /48)
  // can make a few an hour. An installation registers once, and retries a 429.
  registrationsPerMinute: 6,
  registrationsPerHourPerClient: 10,
  // Requests that fail to authenticate, per client address, before it is
  // turned away for the rest of the minute.
  failedAuthPerMinutePerClient: 120,
  // Open long-polls, process-wide, and per inbox so one owner can't hold them all.
  maxWaiters: 2_000,
  maxWaitersPerInbox: 8,
  // An inbox nobody has read for this long is deleted with what it holds.
  inboxIdleDays: 90,
});

const VARIABLES = Object.freeze({
  maxEnvelopeBytes: 'MAILBOX_MAX_ENVELOPE_BYTES',
  ttlSeconds: 'MAILBOX_TTL_SECONDS',
  maxWaitSeconds: 'MAILBOX_MAX_WAIT_SECONDS',
  maxInboxes: 'MAILBOX_MAX_INBOXES',
  maxSendersPerInbox: 'MAILBOX_MAX_SENDERS_PER_INBOX',
  maxPendingPerInbox: 'MAILBOX_MAX_PENDING_PER_INBOX',
  maxPendingBytesPerInbox: 'MAILBOX_MAX_PENDING_BYTES_PER_INBOX',
  maxPendingPerSender: 'MAILBOX_MAX_PENDING_PER_SENDER',
  maxTotalBytes: 'MAILBOX_MAX_TOTAL_BYTES',
  sendsPerMinutePerSender: 'MAILBOX_SENDS_PER_MINUTE_PER_SENDER',
  ownerRequestsPerMinutePerInbox: 'MAILBOX_OWNER_REQUESTS_PER_MINUTE_PER_INBOX',
  registrationsPerMinute: 'MAILBOX_REGISTRATIONS_PER_MINUTE',
  registrationsPerHourPerClient: 'MAILBOX_REGISTRATIONS_PER_HOUR_PER_CLIENT',
  failedAuthPerMinutePerClient: 'MAILBOX_FAILED_AUTH_PER_MINUTE_PER_CLIENT',
  maxWaiters: 'MAILBOX_MAX_WAITERS',
  maxWaitersPerInbox: 'MAILBOX_MAX_WAITERS_PER_INBOX',
  inboxIdleDays: 'MAILBOX_INBOX_IDLE_DAYS',
});

// Unset or empty variables keep the default (Compose passes `${VAR:-}` as an
// empty string); anything else must be a positive integer, so a typo fails at
// boot instead of silently disabling a bound.
export function limitsFromEnv(env = process.env) {
  const values = { ...DEFAULT_LIMITS };
  for (const [key, name] of Object.entries(VARIABLES)) {
    const raw = env[name];
    if (raw === undefined || raw.trim() === '') continue;
    const value = /^\d+$/.test(raw.trim()) ? Number(raw.trim()) : NaN;
    if (!Number.isSafeInteger(value) || value <= 0) throw new Error(`${name} must be a positive integer.`);
    values[key] = value;
  }
  return values;
}

// How many inboxes the registration rate can make before the first of them
// would be deleted as idle. With open registration, `maxInboxes` below this
// lets one party with enough addresses fill the mailbox by registering alone.
export function inboxesRegistrationCanFill(limits) {
  return limits.registrationsPerMinute * 60 * 24 * limits.inboxIdleDays;
}
