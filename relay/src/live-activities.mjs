// Lock Screen progress for a running Hermes turn (hermes-conduit #605).
//
// Conduit starts a Live Activity when the user sends a turn and registers its
// ActivityKit push token here under the chat's session ids. The Hermes
// plugin reports the turn's progress for a session id (POST /v1/live-activity),
// and the relay turns it into a `liveactivity` push to that token.
//
// What crosses the relay is coarse on purpose: a phase, a tool category from
// a fixed list, a tool count and a context percentage. iOS renders Live
// Activity payloads itself, so they can't be sealed end to end like
// notifications; the chat's title and anything about the turn's content stay
// on the phone (the activity's static attributes). Nothing here is persisted:
// a restart forgets every registration and the activities go stale on the
// phone (Conduit sets a stale date), which is all that is lost.

export const LIVE_ACTIVITIES_CAPABILITY = 'live-activities';

// An ActivityKit activity runs for at most 8 hours, then iOS ends it.
export const ACTIVITY_TTL_MS = 8 * 60 * 60 * 1000;
// Updates of one activity closer together than this are merged: the latest
// state goes out when the interval is up. Ends go out at once.
export const MIN_UPDATE_INTERVAL_MS = 5_000;
// An activity whose last update is this old shows as stale on the phone.
export const STALE_AFTER_S = 15 * 60;
// A finished turn stays on the Lock Screen this long.
export const DISMISS_AFTER_END_S = 5 * 60;
// An update nobody received within this long isn't worth delivering.
const UPDATE_EXPIRY_S = 5 * 60;

export const PHASES = new Set(['thinking', 'tool', 'approval', 'input']);
export const ENDS = new Set(['done', 'failed', 'stopped']);
export const TOOL_CATEGORIES = new Set([
  'tool', 'terminal', 'code', 'files', 'web', 'browser', 'agents', 'memory', 'images', 'audio',
  'planning', 'schedule', 'messages', 'skills', 'connector', 'phone', 'home',
]);
const MAX_SESSION_IDS = 6;
// Tries at an end push before the activity is given up on.
const END_ATTEMPTS = 3;
const SESSION_ID_PATTERN = /^[A-Za-z0-9:_.\/-]{1,128}$/;
const TOKEN_GONE = new Set(['BadDeviceToken', 'Unregistered', 'DeviceTokenNotForTopic', 'ExpiredToken']);

export class LiveActivities {
  constructor({ maxActivities, maxPerInstallation, send, now = Date.now, minIntervalMs = MIN_UPDATE_INTERVAL_MS, warn = () => {} }) {
    this.maxActivities = maxActivities;
    this.maxPerInstallation = maxPerInstallation;
    this.send = send;
    this.now = now;
    this.minIntervalMs = minIntervalMs;
    this.warn = warn;
    // token -> activity; installationId -> Set(token); `${installationId}\n${sessionId}` -> token
    this.activities = new Map();
    this.byInstallation = new Map();
    this.bySession = new Map();
  }

  get size() {
    return this.activities.size;
  }

  // The phone started an activity for a chat known under `sessionIds`.
  register(installationId, token, sessionIds) {
    this.sweep();
    this.remove(token);
    const activity = {
      token,
      installationId,
      sessionIds: [...new Set(sessionIds)].slice(0, MAX_SESSION_IDS),
      registeredAt: this.now(),
      lastSentAt: 0,
      pending: null,
      timer: null,
      sending: false,
      ending: false,
      endAttempts: 0,
    };
    const tokens = this.byInstallation.get(installationId) ?? new Set();
    // The oldest goes first: a phone shows only a few at once anyway.
    while (tokens.size >= this.maxPerInstallation) this.remove(tokens.values().next().value);
    while (this.activities.size >= this.maxActivities) this.remove(this.activities.keys().next().value);
    this.activities.set(token, activity);
    tokens.add(token);
    this.byInstallation.set(installationId, tokens);
    // A newer registration for the same chat takes the id over.
    for (const sessionId of activity.sessionIds) this.bySession.set(sessionKey(installationId, sessionId), token);
    return activity;
  }

  // The phone ended the activity (or the token stopped working). Only the
  // installation that registered it may remove it by name.
  unregister(installationId, token) {
    const activity = this.activities.get(token);
    if (!activity || activity.installationId !== installationId) return false;
    this.remove(token);
    return true;
  }

  find(installationId, sessionId) {
    const token = this.bySession.get(sessionKey(installationId, sessionId));
    const activity = token ? this.activities.get(token) : undefined;
    if (!activity) return undefined;
    if (this.now() - activity.registeredAt > ACTIVITY_TTL_MS) {
      this.remove(token);
      return undefined;
    }
    return activity;
  }

  // A progress report from the host. Returns whether the phone is watching.
  report(installationId, sessionId, state, end) {
    const activity = this.find(installationId, sessionId);
    if (!activity) return false;
    // Once the end is on its way, nothing after it may overtake it.
    if (activity.ending || activity.pending?.end) return true;
    activity.pending = { state, end };
    this.schedule(activity);
    return true;
  }

  schedule(activity) {
    if (activity.pending?.end) {
      clearTimeout(activity.timer);
      activity.timer = null;
      // An end waits for the update in flight (it settles and reschedules),
      // so APNs never gets the end before the last update.
      this.flush(activity);
      return;
    }
    // The send in flight schedules the rest when it settles.
    if (activity.sending || activity.timer) return;
    const wait = activity.lastSentAt + this.minIntervalMs - this.now();
    if (wait <= 0) {
      this.flush(activity);
      return;
    }
    activity.timer = setTimeout(() => {
      activity.timer = null;
      this.flush(activity);
    }, wait);
    activity.timer.unref?.();
  }

  flush(activity) {
    // A registration that replaced this one (same token) isn't ours to send.
    if (!activity.pending || this.activities.get(activity.token) !== activity) return;
    // The send in flight reschedules the rest when it settles.
    if (activity.sending) return;
    const { state, end } = activity.pending;
    activity.pending = null;
    activity.lastSentAt = this.now();
    activity.sending = true;
    if (end) activity.ending = true;
    const push = liveActivityPush(state, end, { nowSeconds: Math.floor(this.now() / 1000) });
    let delivered = false;
    let gone = false;
    Promise.resolve()
      .then(() => this.send(activity.token, push))
      .then((result) => {
        if (result && !result.ok) {
          this.warn('live activity push refused', { status: result.status, reason: result.reason });
          gone = result.status === 410 || TOKEN_GONE.has(result.reason);
        } else {
          delivered = true;
        }
      })
      .catch((error) => this.warn('live activity push failed', { error: error instanceof Error ? error.message : String(error) }))
      .finally(() => {
        activity.sending = false;
        // Re-registered under the same token meanwhile: the new
        // registration is left alone.
        if (this.activities.get(activity.token) !== activity) return;
        if (gone) {
          this.remove(activity.token);
          return;
        }
        if (end) {
          // The end is kept until APNs took it: a failed one is tried again
          // a few times before the activity's stale date has to do.
          activity.endAttempts = (activity.endAttempts ?? 0) + 1;
          if (delivered || activity.endAttempts >= END_ATTEMPTS) {
            this.remove(activity.token);
            return;
          }
          activity.pending = { state, end };
          activity.timer = setTimeout(() => {
            activity.timer = null;
            this.flush(activity);
          }, this.minIntervalMs);
          activity.timer.unref?.();
          return;
        }
        if (activity.pending) this.schedule(activity);
      });
  }

  remove(token) {
    const activity = this.activities.get(token);
    if (!activity) return;
    clearTimeout(activity.timer);
    activity.timer = null;
    this.activities.delete(token);
    const tokens = this.byInstallation.get(activity.installationId);
    tokens?.delete(token);
    if (tokens && tokens.size === 0) this.byInstallation.delete(activity.installationId);
    for (const sessionId of activity.sessionIds) {
      const key = sessionKey(activity.installationId, sessionId);
      if (this.bySession.get(key) === token) this.bySession.delete(key);
    }
  }

  removeInstallation(installationId) {
    for (const token of [...(this.byInstallation.get(installationId) ?? [])]) this.remove(token);
  }

  sweep() {
    const now = this.now();
    for (const [token, activity] of this.activities) {
      // Insertion order is registration order: the first live one ends the sweep.
      if (now - activity.registeredAt <= ACTIVITY_TTL_MS) break;
      this.remove(token);
    }
  }
}

function sessionKey(installationId, sessionId) {
  return `${installationId}\n${sessionId}`;
}

// The APNs request for one update or end. `content-state` must decode as
// Conduit's HermesTurnAttributes.ContentState: keep the keys in step.
export function liveActivityPush(state, end, { topic = undefined, nowSeconds = Math.floor(Date.now() / 1000) } = {}) {
  const contentState = {
    phase: end ?? state.phase,
    tools: state.tools ?? 0,
    ...(state.tool && !end ? { tool: state.tool } : {}),
    ...(state.context !== undefined ? { context: state.context } : {}),
    ...(end ? { endedAt: nowSeconds } : {}),
  };
  const aps = end
    ? { timestamp: nowSeconds, event: 'end', 'content-state': contentState, 'dismissal-date': nowSeconds + DISMISS_AFTER_END_S }
    : { timestamp: nowSeconds, event: 'update', 'content-state': contentState, 'stale-date': nowSeconds + STALE_AFTER_S };
  // Waiting on the user and the end are worth showing right away; the rest
  // rides APNs' low priority, which doesn't count against the app's budget.
  const urgent = Boolean(end) || state.phase === 'approval' || state.phase === 'input';
  return {
    payload: { aps },
    pushType: 'liveactivity',
    ...(topic ? { topic } : {}),
    priority: urgent ? 10 : 5,
    expiration: nowSeconds + (end ? DISMISS_AFTER_END_S : UPDATE_EXPIRY_S),
  };
}

// The body of POST /v1/live-activity, or null when malformed.
export function validateReport(body) {
  const sessionId = typeof body?.session_id === 'string' && SESSION_ID_PATTERN.test(body.session_id) ? body.session_id : null;
  const raw = body?.state;
  if (!sessionId || !raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const end = body.end === undefined || body.end === null ? undefined : (ENDS.has(body.end) ? body.end : null);
  if (end === null) return null;
  if (!PHASES.has(raw.phase)) return null;
  const tools = raw.tools === undefined || raw.tools === null ? 0 : boundedInteger(raw.tools, 0, 10_000);
  if (tools === undefined) return null;
  const state = { phase: raw.phase, tools };
  if (raw.tool !== undefined && raw.tool !== null) {
    if (!TOOL_CATEGORIES.has(raw.tool)) return null;
    state.tool = raw.tool;
  }
  if (raw.context !== undefined && raw.context !== null) {
    const context = boundedInteger(raw.context, 0, 100);
    if (context === undefined) return null;
    state.context = context;
  }
  return { sessionId, state, end };
}

// The body of the phone's registration: its activity token and the chat's
// session ids (runtime, stored, lineage), or null when malformed.
export function validateRegistration(body) {
  const token = String(body?.push_token ?? '').replace(/[<>\s]/g, '').toLowerCase();
  if (!/^[0-9a-f]{64,400}$/.test(token)) return null;
  if (!Array.isArray(body.session_ids)) return null;
  if (!body.session_ids.every((id) => typeof id === 'string' && SESSION_ID_PATTERN.test(id))) return null;
  const sessionIds = [...new Set(body.session_ids)];
  if (sessionIds.length === 0 || sessionIds.length > MAX_SESSION_IDS) return null;
  return { token, sessionIds };
}

export function validateToken(body) {
  const token = String(body?.push_token ?? '').replace(/[<>\s]/g, '').toLowerCase();
  return /^[0-9a-f]{64,400}$/.test(token) ? token : null;
}

function boundedInteger(value, min, max) {
  return Number.isInteger(value) && value >= min && value <= max ? value : undefined;
}
