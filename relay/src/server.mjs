import { createServer } from 'node:http';
import { readFileSync, realpathSync } from 'node:fs';
import { isIP } from 'node:net';
import { pathToFileURL } from 'node:url';
import { ApnsClient } from './apns.mjs';
import { RelayStore } from './store.mjs';

// Self-reported relay version/capabilities, surfaced via GET /v1/meta so the
// app can show compatibility state (keep the version in sync with package.json).
const RELAY_INFO = (() => {
  try {
    const pkg = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8'));
    return { version: String(pkg.version || 'unknown'), capabilities: ['decisions', 'decision-cards', 'meta'] };
  } catch {
    return { version: 'unknown', capabilities: ['decisions', 'decision-cards', 'meta'] };
  }
})();

let config;
let store;
let apns;
let limits;

// APNs hard-caps a notification payload at 4096 bytes; stay under it with
// headroom for JSON escaping and delivery headers, dropping decision content
// (not the notification) when a payload would exceed the bound.
const MAX_NOTIFICATION_BYTES = 3800;

function main() {
  config = readConfig();
  store = new RelayStore(config.dataPath);
  apns = new ApnsClient(config);
  limits = new Map();

  const server = createServer(async (request, response) => {
    const startedAt = Date.now();
    try {
      await route(request, response);
    } catch (error) {
      const status = Number(error?.status ?? 500);
      const code = status < 500 && error instanceof Error ? error.message : 'internal_error';
      if (status >= 500) console.error(JSON.stringify({ level: 'error', message: 'request failed', error: error instanceof Error ? error.message : String(error) }));
      sendJson(response, status, { error: code });
    } finally {
      console.log(JSON.stringify({ level: 'info', method: request.method, path: safePath(request.url), status: response.statusCode, duration_ms: Date.now() - startedAt }));
    }
  });

  server.listen(config.port, config.host, () => {
    console.log(JSON.stringify({ level: 'info', message: 'Conduit push relay listening', host: config.host, port: config.port, public_url: config.publicUrl }));
  });

  process.on('SIGTERM', () => server.close(() => process.exit(0)));
  process.on('SIGINT', () => server.close(() => process.exit(0)));
}

// Start the server only when executed directly, so the pure notification
// builders can be unit-tested by importing this module without binding a port.
// Resolve argv[1] through realpath so a symlinked entrypoint still matches
// import.meta.url (which Node resolves to the real file). If an entrypoint
// exists but does not match, say so on stderr — a silent no-op start is the
// worst possible deployment failure mode.
function isEntryPoint() {
  if (!process.argv[1]) return false;
  try {
    return pathToFileURL(realpathSync(process.argv[1])).href === import.meta.url;
  } catch {
    return false;
  }
}
if (isEntryPoint()) {
  main();
} else if (process.argv[1] && !process.env.NODE_TEST_CONTEXT) {
  console.error(JSON.stringify({
    level: 'warn',
    message: 'relay module imported without matching entrypoint; not starting server',
    argv1: process.argv[1],
    module: import.meta.url,
  }));
}

async function route(request, response) {
  const url = new URL(request.url ?? '/', config.publicUrl);
  const client = clientAddress(request);
  if (request.method === 'GET' && url.pathname === '/healthz') return sendJson(response, 200, { ok: true });

  if (request.method === 'POST' && url.pathname === '/v1/installations') {
    enforceRateLimit(`registration:${client}`, 12, 60_000);
    const body = await readJson(request);
    const deviceToken = validateDeviceToken(body.device_token);
    if (body.bundle_id !== config.topic) return sendJson(response, 400, { error: 'invalid_topic' });
    if (body.environment !== 'production') return sendJson(response, 400, { error: 'production_only' });
    const created = store.createInstallation({ bundleId: body.bundle_id, deviceToken, environment: body.environment, preferences: body.preferences });
    return sendJson(response, 201, {
      installation: created.installation,
      credential: `${created.installation.id}.${created.deviceSecret}`,
      relay_url: config.publicUrl,
    });
  }

  const installationMatch = url.pathname.match(/^\/v1\/installations\/([0-9a-f-]+)$/i);
  if (installationMatch && request.method === 'PUT') {
    const installation = authorize(request, installationMatch[1], 'device');
    if (!installation) return sendJson(response, 401, { error: 'unauthorized' });
    const body = await readJson(request);
    const deviceToken = body.device_token === undefined ? undefined : validateDeviceToken(body.device_token);
    return sendJson(response, 200, { installation: store.updateInstallation(installation.id, { deviceToken, preferences: body.preferences }) });
  }
  if (installationMatch && request.method === 'DELETE') {
    const installation = authorize(request, installationMatch[1], 'device');
    if (!installation) return sendJson(response, 401, { error: 'unauthorized' });
    store.deactivateInstallation(installation.id);
    response.writeHead(204).end();
    return;
  }

  const pairingMatch = url.pathname.match(/^\/v1\/installations\/([0-9a-f-]+)\/pairings$/i);
  if (pairingMatch && request.method === 'POST') {
    const installation = authorize(request, pairingMatch[1], 'device');
    if (!installation) return sendJson(response, 401, { error: 'unauthorized' });
    enforceRateLimit(`pairing:${installation.id}`, 5, 60_000);
    const pairing = store.createPairing(installation.id);
    return sendJson(response, 201, { pairing_code: pairing.code, expires_at: pairing.expiresAt });
  }

  if (request.method === 'POST' && url.pathname === '/v1/pairings/claim') {
    enforceRateLimit(`claim:${client}`, 20, 60_000);
    const body = await readJson(request);
    const claimed = store.claimPairing(body.pairing_code, body.gateway_name);
    if (!claimed) return sendJson(response, 404, { error: 'invalid_or_expired_pairing' });
    return sendJson(response, 200, {
      installation_id: claimed.installationId,
      gateway_id: claimed.gatewayId,
      credential: `${claimed.installationId}.${claimed.gatewayId}.${claimed.gatewaySecret}`,
      relay_url: config.publicUrl,
    });
  }

  if (request.method === 'POST' && url.pathname === '/v1/events') {
    const credential = gatewayCredential(request);
    if (!credential) return sendJson(response, 401, { error: 'unauthorized' });
    const authenticated = store.authenticateGateway(credential.installationId, credential.gatewayId, credential.secret);
    if (!authenticated) return sendJson(response, 401, { error: 'unauthorized' });
    const { installation } = authenticated;
    enforceRateLimit(`event:${installation.id}`, 30, 60_000);
    const body = await readJson(request);
    const event = validateEvent(body);
    // Plugin version recording runs BEFORE the dedupe return: a second
    // gateway on the same installation running the same plugin version sends
    // the same deterministic plugin.hello id, and it must still be recorded.
    // Re-recording identical state is idempotent, unlike pending decisions.
    if (event.pluginVersion) {
      store.recordGatewayPlugin(installation.id, credential.gatewayId, {
        version: event.pluginVersion,
        capabilities: event.pluginCapabilities,
      });
    }
    if (!store.acceptEvent(installation.id, event.eventId)) return sendJson(response, 200, { accepted: true, duplicate: true });
    // Control event: version announcement only, never a notification.
    if (event.type === 'plugin.hello') return sendJson(response, 202, { accepted: true, delivered: false });
    // A clarify decision carries a plugin-minted request id; park it so the
    // device can answer by id and the gateway can poll for the answer while
    // its middleware blocks the tool call. Saved after the dedupe check so a
    // re-delivered event can never overwrite (and wipe the answer of) the
    // already-parked decision. `deliverable` records whether device
    // preferences will actually show an answerable card, so the plugin can
    // stop polling a decision nobody can answer.
    if (event.decision?.kind === 'clarify' && event.decision.request_id) {
      store.savePendingDecision({
        id: event.decision.request_id,
        installationId: installation.id,
        gatewayId: credential.gatewayId,
        question: event.decision.question,
        choices: event.decision.choices,
        deliverable: shouldDeliver(installation.preferences, event.type)
          && installation.preferences.decision_cards !== false,
      });
    }
    if (!shouldDeliver(installation.preferences, event.type)) return sendJson(response, 202, { accepted: true, delivered: false });
    const notification = notificationFor(event, installation.preferences);
    const result = await apns.send(installation.deviceToken, notification);
    if (result.status === 410 || result.reason === 'BadDeviceToken' || result.reason === 'Unregistered') store.deactivateInstallation(installation.id);
    if (!result.ok) return sendJson(response, 502, { error: 'apns_rejected', reason: result.reason, status: result.status });
    return sendJson(response, 202, { accepted: true, delivered: true });
  }

  // ── Clarify answer loop ──────────────────────────────────────────────
  const decisionMatch = url.pathname.match(/^\/v1\/decisions\/([A-Za-z0-9_-]{4,128})$/);
  if (decisionMatch && request.method === 'POST') {
    // The device that received the push answers by the plugin-minted id.
    // Authenticate and rate-limit before reading the body: this endpoint is
    // publicly reachable, and unauthenticated request bodies must never be
    // parsed (matches the pre-auth posture of /v1/pairings/claim).
    const id = decisionMatch[1];
    enforceRateLimit(`decision-respond-ip:${client}`, 60, 60_000);
    const credential = bearerCredential(request);
    if (!credential) return sendJson(response, 401, { error: 'unauthorized' });
    const installation = store.authenticate(credential.id, credential.secret, 'device');
    if (!installation) return sendJson(response, 401, { error: 'unauthorized' });
    enforceRateLimit(`decision-respond:${installation.id}`, 30, 60_000);
    const body = await readJson(request);
    const answer = cleanText(body.answer, 2000);
    if (!answer) return sendJson(response, 400, { error: 'invalid_answer' });
    const outcome = store.respondPendingDecision(installation.id, id, answer);
    if (outcome === 'unknown') return sendJson(response, 404, { error: 'unknown_decision' });
    if (outcome === 'already_answered') return sendJson(response, 409, { error: 'already_answered' });
    return sendJson(response, 200, { status: 'answered' });
  }
  if (decisionMatch && request.method === 'GET') {
    // The gateway polls for the answer while its clarify middleware blocks.
    const credential = gatewayCredential(request);
    if (!credential || !store.authenticateGateway(credential.installationId, credential.gatewayId, credential.secret)) return sendJson(response, 401, { error: 'unauthorized' });
    enforceRateLimit(`decision-poll:${credential.gatewayId}`, 120, 60_000);
    const status = store.pendingDecisionStatus(credential.installationId, credential.gatewayId, decisionMatch[1]);
    return sendJson(response, 200, status);
  }

  if (request.method === 'DELETE' && url.pathname === '/v1/gateways/current') {
    const credential = gatewayCredential(request);
    if (!credential || !store.authenticateGateway(credential.installationId, credential.gatewayId, credential.secret)) return sendJson(response, 401, { error: 'unauthorized' });
    store.removeGateway(credential.installationId, credential.gatewayId);
    response.writeHead(204).end();
    return;
  }

  // Compatibility view for Settings > Notifications: the relay's own
  // version/capabilities plus each paired gateway's last-seen plugin state.
  // Devices authenticate with their installation credential; older relays
  // without this endpoint simply 404 and the app renders an unknown state.
  if (request.method === 'GET' && url.pathname === '/v1/meta') {
    enforceRateLimit(`meta-ip:${client}`, 60, 60_000);
    const credential = bearerCredential(request);
    if (!credential) return sendJson(response, 401, { error: 'unauthorized' });
    const installation = store.authenticate(credential.id, credential.secret, 'device');
    if (!installation) return sendJson(response, 401, { error: 'unauthorized' });
    enforceRateLimit(`meta:${installation.id}`, 30, 60_000);
    const gateways = Object.values(installation.gateways ?? {}).map((gateway) => ({
      id: gateway.id,
      name: gateway.name,
      plugin_version: gateway.pluginVersion,
      plugin_capabilities: gateway.pluginCapabilities ?? [],
      last_event_at: gateway.lastEventAt,
    }));
    return sendJson(response, 200, {
      version: RELAY_INFO.version,
      capabilities: RELAY_INFO.capabilities,
      gateways,
    });
  }

  sendJson(response, 404, { error: 'not_found' });
}

function authorize(request, id, scope) {
  const credential = bearerCredential(request);
  if (!credential || credential.id !== id) return null;
  return store.authenticate(id, credential.secret, scope);
}

function bearerCredential(request) {
  const match = String(request.headers.authorization ?? '').match(/^Bearer\s+([0-9a-f-]+)\.([A-Za-z0-9_-]+)$/i);
  return match ? { id: match[1], secret: match[2] } : null;
}

function gatewayCredential(request) {
  const match = String(request.headers.authorization ?? '').match(/^Bearer\s+([0-9a-f-]+)\.([0-9a-f-]+)\.([A-Za-z0-9_-]+)$/i);
  return match ? { installationId: match[1], gatewayId: match[2], secret: match[3] } : null;
}

function notificationFor(event, preferences) {
  const generic = genericCopy(event.type);
  const title = preferences.show_previews && event.title ? event.title : generic.title;
  // Keep previews private by default, while still making notifications from
  // different profiles and sessions distinguishable in Notification Center.
  const body = preferences.show_previews && event.body ? event.body : notificationContext(generic.body, event);
  const completion = event.type === 'response.ready' || event.type === 'background_task.finished';
  // `decision` carries structured approval card content so Conduit can render
  // an answerable card from the push payload alone — the one-shot gateway
  // stream event is missed while the app is backgrounded. It has its own
  // dedicated `decision_cards` preference (default on, independent of
  // show_previews): previews only control banner text, while this controls
  // functional answerability, and the audience running approval gates is
  // exactly who the cards are for. `!== false` keeps legacy installations
  // (whose stored preferences predate the key) on the default.
  const decision = preferences.decision_cards !== false
    ? validateDecision(event.decision, event.type)
    : undefined;
  const routing = {
    type: event.type,
    session_id: event.sessionId,
    profile: event.profile,
    gateway: event.gateway,
  };
  const conduit = { ...routing, ...(decision ? { decision } : {}) };
  const aps = {
    alert: { title, body },
    ...(preferences.completion_sound && completion ? { sound: 'default' } : {}),
    'thread-id': event.sessionId ?? 'hermes',
  };
  // expo-notifications on iOS exposes the top-level APNs `body` value as
  // `notification.request.content.data`. Keep the Conduit route there so
  // a notification tap can recover its session/profile after a cold start.
  // The top-level copy remains for clients that read the raw APNs payload.
  let payload = { aps, body: { conduit }, conduit };
  // APNs caps the notification payload at 4 KB and rejects anything larger.
  // A maximal description (500 chars of multi-byte UTF-8, echoed in both
  // conduit copies) plus the alert copy can approach that, so degrade to the
  // routing stub instead of losing the whole notification. The headroom under
  // the 4096 cap absorbs per-request APNs headers and JSON escaping.
  if (decision && Buffer.byteLength(JSON.stringify(payload)) > MAX_NOTIFICATION_BYTES) {
    payload = { aps, body: { conduit: routing }, conduit: routing };
  }
  return {
    collapseId: event.sessionId ? `${event.type}:${event.sessionId}` : event.eventId,
    payload,
  };
}

function notificationContext(message, event) {
  const profile = String(event.profile ?? '').trim();
  const sessionId = String(event.sessionId ?? '').trim();
  const details = [profile && `Profile: ${profile}`, sessionId && `Session: ${shortSessionId(sessionId)}`].filter(Boolean);
  return details.length ? `${message}\n${details.join(' · ')}` : message;
}

function shortSessionId(sessionId) {
  return sessionId.length > 10 ? `…${sessionId.slice(-8)}` : sessionId;
}

function genericCopy(type) {
  if (type === 'approval.needed') return { title: 'Approval needed', body: 'Hermes is waiting for your approval.' };
  if (type === 'input.needed') return { title: 'Input needed', body: 'Hermes needs your response before it can continue.' };
  if (type === 'turn.failed') return { title: 'Turn failed', body: 'A Hermes turn could not be completed.' };
  if (type === 'background_task.finished') return { title: 'Background task finished', body: 'A delegated task has finished.' };
  return { title: 'Response ready', body: 'Hermes has finished responding.' };
}

function shouldDeliver(preferences, type) {
  if (!preferences.enabled) return false;
  return preferences[type.replace('.', '_')] !== false;
}

function validateEvent(body) {
  const types = new Set(['approval.needed', 'input.needed', 'response.ready', 'turn.failed', 'background_task.finished', 'plugin.hello']);
  if (!types.has(body.type)) throw httpError(400, 'invalid_event_type');
  const eventId = String(body.event_id ?? '');
  if (!/^[A-Za-z0-9:_-]{8,128}$/.test(eventId)) throw httpError(400, 'invalid_event_id');
  return {
    eventId,
    type: body.type,
    title: cleanText(body.title, 120),
    body: cleanText(body.body, 500),
    sessionId: cleanIdentifier(body.session_id, 180),
    profile: cleanText(body.profile, 80),
    gateway: cleanIdentifier(body.gateway, 80),
    pluginVersion: cleanIdentifier(body.plugin_version, 40),
    pluginCapabilities: Array.isArray(body.plugin_capabilities)
      ? body.plugin_capabilities.map((capability) => cleanIdentifier(capability, 40)).filter((capability) => capability !== undefined).slice(0, 16)
      : [],
    decision: validateDecision(body.decision, body.type),
  };
}

// Mirror the plugin's decision sanitization at the relay trust boundary.
// Two contracts, each bound to its event type:
// - approval: session-keyed, answered via the gateway's approval.respond;
//   choices whitelisted to the gateway's approval vocabulary.
// - clarify: keyed by a plugin-minted request id (the gateway's own clarify
//   id is unreachable to plugins), answered via this relay's /v1/decisions
//   endpoints. Choices are model-generated labels, so they are bounded but
//   deliberately not vocabulary-whitelisted.
// Anything malformed degrades to undefined and the notification ships as a
// routing stub.
function validateDecision(value, eventType) {
  if (!value || typeof value !== 'object') return undefined;
  const kind = cleanText(value.kind, 40);
  if (kind === 'approval' && eventType === 'approval.needed') {
    const sessionKey = cleanIdentifier(value.session_key, 180);
    const description = cleanText(value.description, 500);
    if (!sessionKey || !description) return undefined;
    const allowed = new Set(['once', 'session', 'always', 'deny']);
    const choices = [...new Set(
      (Array.isArray(value.choices) ? value.choices : [])
        .map((choice) => cleanText(choice, 80))
        .filter((choice) => allowed.has(choice)),
    )];
    if (!choices.length) return undefined;
    return { kind: 'approval', session_key: sessionKey, description, choices };
  }
  if (kind === 'clarify' && eventType === 'input.needed') {
    const requestId = cleanIdentifier(value.request_id, 128);
    const question = cleanText(value.question, 500);
    if (!requestId || !question) return undefined;
    const choices = (Array.isArray(value.choices) ? value.choices : [])
      .map((choice) => cleanText(choice, 80))
      .filter((choice) => choice !== undefined)
      .slice(0, 8);
    return { kind: 'clarify', request_id: requestId, question, ...(choices.length ? { choices } : {}) };
  }
  return undefined;
}

function validateDeviceToken(value) {
  const token = String(value ?? '').replace(/[<>\s]/g, '').toLowerCase();
  if (!/^[0-9a-f]{64,200}$/.test(token)) throw httpError(400, 'invalid_device_token');
  return token;
}

function cleanText(value, max) {
  return typeof value === 'string' ? value.replace(/[\u0000-\u001f]/g, ' ').trim().slice(0, max) || undefined : undefined;
}

function cleanIdentifier(value, max) {
  return typeof value === 'string' && /^[A-Za-z0-9:_.\/-]+$/.test(value) ? value.slice(0, max) : undefined;
}

function readJson(request) {
  return new Promise((resolve, reject) => {
    let data = '';
    request.setEncoding('utf8');
    request.on('data', (chunk) => {
      data += chunk;
      if (data.length > 32_768) { reject(httpError(413, 'payload_too_large')); request.destroy(); }
    });
    request.on('end', () => {
      try { resolve(data ? JSON.parse(data) : {}); } catch { reject(httpError(400, 'invalid_json')); }
    });
    request.on('error', reject);
  });
}

function sendJson(response, status, body) {
  if (response.headersSent) return;
  const encoded = JSON.stringify(body);
  response.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'content-length': Buffer.byteLength(encoded), 'cache-control': 'no-store', 'x-content-type-options': 'nosniff' });
  response.end(encoded);
}

let lastLimitsSweepAt = 0;

function enforceRateLimit(key, maximum, windowMs) {
  const now = Date.now();
  // The map never evicts on its own and some keys (per-IP buckets) are
  // attacker-rotatable. Sweep expired entries once it grows large, but at
  // most once per 30s so a sustained flood cannot turn every request into a
  // full-map scan.
  if (limits.size > 10_000 && now - lastLimitsSweepAt > 30_000) {
    lastLimitsSweepAt = now;
    for (const [limitKey, limit] of limits) {
      if (limit.resetAt <= now) limits.delete(limitKey);
    }
  }
  const value = limits.get(key);
  if (!value || value.resetAt <= now) { limits.set(key, { count: 1, resetAt: now + windowMs }); return; }
  value.count += 1;
  if (value.count > maximum) throw httpError(429, 'rate_limited');
}

function clientAddress(request) {
  if (config.trustProxy) {
    const forwarded = String(request.headers['x-forwarded-for'] ?? '').split(',')[0].trim();
    if (isIP(forwarded)) return forwarded;
  }
  return request.socket.remoteAddress ?? 'unknown';
}

function httpError(status, code) {
  const error = new Error(code);
  error.status = status;
  return error;
}

function safePath(value) {
  try { return new URL(value ?? '/', config.publicUrl).pathname; } catch { return '/invalid'; }
}

function readConfig() {
  const required = ['PUBLIC_URL', 'DATA_PATH', 'APNS_KEY_PATH', 'APNS_KEY_ID', 'APNS_TEAM_ID', 'APNS_TOPIC'];
  for (const name of required) if (!process.env[name]) throw new Error(`${name} is required.`);
  const publicUrl = new URL(process.env.PUBLIC_URL);
  if (publicUrl.protocol !== 'https:') throw new Error('PUBLIC_URL must use HTTPS.');
  return {
    host: process.env.HOST || '127.0.0.1',
    port: Number(process.env.PORT || 9120),
    publicUrl: publicUrl.toString().replace(/\/$/, ''),
    dataPath: process.env.DATA_PATH,
    keyPath: process.env.APNS_KEY_PATH,
    keyId: process.env.APNS_KEY_ID,
    teamId: process.env.APNS_TEAM_ID,
    topic: process.env.APNS_TOPIC,
    trustProxy: process.env.TRUST_PROXY === '1',
  };
}

export { notificationFor, validateEvent, validateDecision };
