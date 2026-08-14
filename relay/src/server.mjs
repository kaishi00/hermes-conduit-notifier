import { createServer } from 'node:http';
import { isIP } from 'node:net';
import { pathToFileURL } from 'node:url';
import { ApnsClient } from './apns.mjs';
import { RelayStore } from './store.mjs';

let config;
let store;
let apns;
let limits;

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
if (pathToFileURL(process.argv[1] ?? '').href === import.meta.url) main();

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
    if (!store.acceptEvent(installation.id, event.eventId)) return sendJson(response, 200, { accepted: true, duplicate: true });
    if (!shouldDeliver(installation.preferences, event.type)) return sendJson(response, 202, { accepted: true, delivered: false });
    const notification = notificationFor(event, installation.preferences);
    const result = await apns.send(installation.deviceToken, notification);
    if (result.status === 410 || result.reason === 'BadDeviceToken' || result.reason === 'Unregistered') store.deactivateInstallation(installation.id);
    if (!result.ok) return sendJson(response, 502, { error: 'apns_rejected', reason: result.reason, status: result.status });
    return sendJson(response, 202, { accepted: true, delivered: true });
  }

  if (request.method === 'DELETE' && url.pathname === '/v1/gateways/current') {
    const credential = gatewayCredential(request);
    if (!credential || !store.authenticateGateway(credential.installationId, credential.gatewayId, credential.secret)) return sendJson(response, 401, { error: 'unauthorized' });
    store.removeGateway(credential.installationId, credential.gatewayId);
    response.writeHead(204).end();
    return;
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
  // `decision` carries structured card content (approval/clarify) so Conduit
  // can render an answerable card from the push payload alone — the one-shot
  // gateway stream event is missed while the app is backgrounded. The plugin
  // already bounds/whitelists it; re-validate here as the trust boundary.
  const decision = validateDecision(event.decision);
  const conduit = {
    type: event.type,
    session_id: event.sessionId,
    profile: event.profile,
    gateway: event.gateway,
    ...(decision ? { decision } : {}),
  };
  return {
    collapseId: event.sessionId ? `${event.type}:${event.sessionId}` : event.eventId,
    payload: {
      aps: {
        alert: { title, body },
        ...(preferences.completion_sound && completion ? { sound: 'default' } : {}),
        'thread-id': event.sessionId ?? 'hermes',
      },
      // expo-notifications on iOS exposes the top-level APNs `body` value as
      // `notification.request.content.data`. Keep the Conduit route there so
      // a notification tap can recover its session/profile after a cold start.
      // The top-level copy remains for clients that read the raw APNs payload.
      body: { conduit },
      conduit,
    },
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
  const types = new Set(['approval.needed', 'input.needed', 'response.ready', 'turn.failed', 'background_task.finished']);
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
    decision: validateDecision(body.decision),
  };
}

// Mirror the plugin's decision sanitization at the relay trust boundary: bound
// field sizes, whitelist the kind, and require both a display field and the
// answerability key (approval: session_key; clarify: request_id). Anything
// malformed degrades to undefined and the notification ships as a routing stub.
function validateDecision(value) {
  if (!value || typeof value !== 'object') return undefined;
  const kind = cleanText(value.kind, 40);
  if (kind !== 'approval' && kind !== 'clarify') return undefined;
  const decision = { kind };
  for (const key of ['session_key', 'request_id', 'question', 'description']) {
    const text = cleanText(value[key], 500);
    if (text) decision[key] = text;
  }
  if (Array.isArray(value.choices)) {
    const choices = value.choices.map((c) => cleanText(c, 80)).filter((c) => c !== undefined);
    if (choices.length) decision.choices = choices.slice(0, 8);
  }
  const hasDisplay = Boolean(decision.description || decision.question);
  const answerable = kind === 'approval' ? decision.session_key : decision.request_id;
  return hasDisplay && answerable ? decision : undefined;
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

function enforceRateLimit(key, maximum, windowMs) {
  const now = Date.now();
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
