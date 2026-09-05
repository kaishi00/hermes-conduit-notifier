import { createServer } from 'node:http';
import { readFileSync, realpathSync } from 'node:fs';
import { isIP } from 'node:net';
import { pathToFileURL } from 'node:url';
import { ApnsClient } from './apns.mjs';
import { RelayStore, sanitizeBatchQuestions } from './store.mjs';

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
let apnsSend;

// APNs hard-caps a notification payload at 4096 bytes; stay under it with
// headroom for JSON escaping and delivery headers, dropping decision content
// (not the notification) when a payload would exceed the bound.
const MAX_NOTIFICATION_BYTES = 3800;

function main() {
  config = readConfig();
  store = new RelayStore(config.dataPath);
  apns = new ApnsClient(config);
  limits = new Map();
  // Test seam: APNS_MODE=accept makes every send succeed, reject makes it
  // return a 403 failure, and throw makes it RAISE (a dropped connection) —
  // all without touching the network — so the e2e suite can exercise
  // delivery outcomes, decision parking, and both APNs-failure shapes
  // (returned rejection and thrown transport error) deterministically.
  // Unset = real APNs.
  if (process.env.APNS_MODE === 'accept') {
    apnsSend = async () => ({ ok: true, status: 200, reason: null });
  } else if (process.env.APNS_MODE === 'reject') {
    apnsSend = async () => ({ ok: false, status: 403, reason: 'InvalidProviderToken' });
  } else if (process.env.APNS_MODE === 'throw') {
    apnsSend = async () => {
      throw new Error('connection dropped');
    };
  } else {
    apnsSend = (deviceToken, notification) => apns.send(deviceToken, notification);
  }

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
    if (!store.acceptEvent(installation.id, event.eventId, credential.gatewayId)) return sendJson(response, 200, { accepted: true, duplicate: true });
    // Control event: version announcement only, never a notification.
    if (event.type === 'plugin.hello') return sendJson(response, 202, { accepted: true, delivered: false });
    // A clarify decision carries a plugin-minted request id; park it so the
    // device can answer by id and the gateway can poll for the answer while
    // its middleware blocks the tool call. Saved after the dedupe check so a
    // re-delivered event can never overwrite (and wipe the answer of) the
    // already-parked decision.
    //
    // Two independent concepts must not be conflated:
    //
    //   notification_delivery  — did the user get the (plain or card-bearing)
    //                            input.needed banner? Governed by the
    //                            ordinary input.needed preference.
    //   relay_decision_deliverability (`deliverable`) — does the delivered
    //                            payload contain an ANSWERABLE structured
    //                            card? Governed by decision_cards preference
    //                            and the APNs size guard.
    //
    // The final notification is built first and `deliverable` is taken from
    // what survived into the APNs payload. A decision whose card was stripped
    // (preference or size guard) still sends the plain banner and is parked
    // undeliverable, so the plugin stops relay-polling and falls back to
    // Hermes' native clarify path — it never suppresses the ordinary
    // notification.
    let parkedDecisionId = null;
    if (event.decision?.kind === 'clarify' && event.decision.request_id) {
      parkedDecisionId = event.decision.request_id;
      const deliverableBase = shouldDeliver(installation.preferences, event.type);
      const notification = deliverableBase
        ? notificationFor(event, installation.preferences)
        : null;
      // The rich body copy is the canonical payload the iOS path reads, and
      // (since the top-level copy became a routing stub) the only place the
      // structured decision lives.
      const decisionDeliverable = notification?.payload?.body?.conduit?.decision != null;
      store.savePendingDecision({
        id: parkedDecisionId,
        installationId: installation.id,
        gatewayId: credential.gatewayId,
        question: event.decision.question,
        choices: event.decision.choices,
        questions: event.decision.questions,
        deliverable: decisionDeliverable,
      });
      if (!notification) return sendJson(response, 202, { accepted: true, delivered: false });
      let result;
      try {
        result = await apnsSend(installation.deviceToken, notification);
      } catch (error) {
        // A THROWN transport failure (dropped connection, DNS, TLS) is the
        // same outcome as a returned rejection: no device received the
        // answerable card, so the parked decision must flip to undeliverable
        // and the failure must still be reported upstream.
        store.markPendingDecisionUndeliverable(installation.id, credential.gatewayId, parkedDecisionId);
        console.error(JSON.stringify({ level: 'error', message: 'apns send threw', error: error instanceof Error ? error.message : String(error) }));
        return sendJson(response, 502, { error: 'apns_unreachable' });
      }
      if (result.status === 410 || result.reason === 'BadDeviceToken' || result.reason === 'Unregistered') store.deactivateInstallation(installation.id);
      if (!result.ok) {
        // The answerable card never reached the device: flip the parked
        // decision to undeliverable so the plugin's next poll falls back to
        // the native clarify path instead of waiting out the budget.
        store.markPendingDecisionUndeliverable(installation.id, credential.gatewayId, parkedDecisionId);
        return sendJson(response, 502, { error: 'apns_rejected', reason: result.reason, status: result.status });
      }
      return sendJson(response, 202, { accepted: true, delivered: true });
    }
    if (!shouldDeliver(installation.preferences, event.type)) return sendJson(response, 202, { accepted: true, delivered: false });
    const notification = notificationFor(event, installation.preferences);
    const result = await apnsSend(installation.deviceToken, notification);
    if (result.status === 410 || result.reason === 'BadDeviceToken' || result.reason === 'Unregistered') store.deactivateInstallation(installation.id);
    if (!result.ok) return sendJson(response, 502, { error: 'apns_rejected', reason: result.reason, status: result.status });
    return sendJson(response, 202, { accepted: true, delivered: true });
  }

  // ── Clarify answer loop ──────────────────────────────────────────────
  // The device answer route is /v1/decisions/{id}/respond (the iOS client's
  // contract); the bare /v1/decisions/{id} POST path is kept as a
  // backward-compatible alias. Both run the same handler.
  const decisionMatch = url.pathname.match(/^\/v1\/decisions\/([A-Za-z0-9_-]{4,128})(\/respond)?$/);
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
    // question_id scopes the answer to ONE question of a batch decision
    // (first-answer-wins per qid, other qids stay open); its absence keeps
    // the legacy whole-decision shape for single-question cards.
    const questionId = cleanText(body.question_id, 40);
    const result = store.respondPendingDecision(installation.id, id, answer, questionId);
    if (result.outcome === 'unknown') return sendJson(response, 404, { error: 'unknown_decision' });
    // Distinct from 404: the decision is live but the sender addressed a qid
    // it does not contain — the request is malformed, not the decision gone.
    if (result.outcome === 'invalid_question') return sendJson(response, 400, { error: 'invalid_question_id' });
    // Distinct device-facing outcomes: a duplicate qid settles only that
    // question as answered elsewhere (409), while a RELEASED decision means
    // Hermes resolved through another surface and the whole pushed card must
    // be torn down (410). Collapsing them would either strand open qids or
    // clear a card that still has answerable questions.
    if (result.outcome === 'released') return sendJson(response, 410, { error: 'decision_released' });
    if (result.outcome === 'already_answered') return sendJson(response, 409, { error: 'already_answered' });
    const payload = { status: 'answered' };
    if (Array.isArray(result.remaining)) payload.remaining = result.remaining;
    return sendJson(response, 200, payload);
  }
  if (decisionMatch && decisionMatch[2] === undefined && request.method === 'GET') {
    // The gateway polls for the answer while its clarify middleware blocks.
    const credential = gatewayCredential(request);
    if (!credential || !store.authenticateGateway(credential.installationId, credential.gatewayId, credential.secret)) return sendJson(response, 401, { error: 'unauthorized' });
    enforceRateLimit(`decision-poll:${credential.gatewayId}`, 120, 60_000);
    const status = store.pendingDecisionStatus(credential.installationId, credential.gatewayId, decisionMatch[1]);
    return sendJson(response, 200, status);
  }
  if (decisionMatch && decisionMatch[2] === undefined && request.method === 'DELETE') {
    // The plugin releases a parked decision when the original clarify path
    // won the race or its poll budget fell back to it: a late device answer
    // must then be rejected (410 decision_released) instead of parking a
    // result nobody reads.
    const credential = gatewayCredential(request);
    if (!credential || !store.authenticateGateway(credential.installationId, credential.gatewayId, credential.secret)) return sendJson(response, 401, { error: 'unauthorized' });
    // Release has its OWN bucket: heavy clarify polling must never be able
    // to starve the DELETE that cleans up when the native path wins.
    enforceRateLimit(`decision-cancel:${credential.gatewayId}`, 30, 60_000);
    const outcome = store.cancelPendingDecision(credential.installationId, credential.gatewayId, decisionMatch[1]);
    if (outcome === 'unknown') return sendJson(response, 404, { error: 'unknown_decision' });
    return sendJson(response, 200, { status: 'cancelled' });
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
  // body.conduit is the canonical rich payload the iOS notification path
  // reads (expo-notifications exposes the APNs `body` value as the
  // notification's data, so a tap can recover session/profile after a cold
  // start — and this is where the structured decision lives). The top-level
  // `conduit` copy stays ROUTING-ONLY for raw-APNs consumers: duplicating
  // the structured decision there once doubled the decision's byte cost and
  // pushed ordinary multi-question batches over the size guard.
  const bodyConduit = { ...routing, ...(decision ? { decision } : {}) };
  const aps = {
    alert: { title, body },
    ...(preferences.completion_sound && completion ? { sound: 'default' } : {}),
    'thread-id': event.sessionId ?? 'hermes',
  };
  let payload = { aps, body: { conduit: bodyConduit }, conduit: routing };
  // APNs caps the notification payload at 4 KB and rejects anything larger.
  // With a single structured copy, the guard now measures the real cost of
  // one decision plus routing and alert copy; a payload that still exceeds
  // the budget degrades to the routing stub instead of losing the whole
  // notification. The headroom under the 4096 cap absorbs per-request APNs
  // headers and JSON escaping.
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
    // Batch decisions (plugin 0.3+) carry the FULL question set. It MUST
    // survive this boundary through the same shared sanitizer the
    // pending-decision store uses — dropping it here would silently reduce
    // every pushed batch to its collapsed first question. Deduplication and
    // bounds are inside the shared sanitizer.
    const questions = sanitizeBatchQuestions(value.questions);
    return {
      kind: 'clarify',
      request_id: requestId,
      question,
      ...(choices.length ? { choices } : {}),
      ...(questions.length ? { questions } : {}),
    };
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
