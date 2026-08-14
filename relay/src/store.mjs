import { createHash, randomBytes, randomUUID } from 'node:crypto';
import { chmodSync, existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';

const defaultPreferences = Object.freeze({
  enabled: true,
  approval_needed: true,
  input_needed: true,
  response_ready: true,
  turn_failed: true,
  background_task_finished: true,
  completion_sound: true,
  show_previews: false,
  // Dedicated opt-out for structured decision content (answerable approval
  // cards) in the push payload. Deliberately independent of show_previews:
  // the feature's audience is exactly the approval-gate crowd, so cards
  // default on and privacy-focused users can turn just this off.
  decision_cards: true,
});

export class RelayStore {
  constructor(path) {
    this.path = path;
    this.data = { version: 1, installations: {}, pairings: {}, eventIds: {}, pendingDecisions: {} };
    this.load();
  }

  load() {
    if (!existsSync(this.path)) return;
    const parsed = JSON.parse(readFileSync(this.path, 'utf8'));
    if (parsed?.version !== 1 || typeof parsed.installations !== 'object') throw new Error('Unsupported relay data format.');
    this.data = { version: 1, installations: parsed.installations ?? {}, pairings: parsed.pairings ?? {}, eventIds: parsed.eventIds ?? {}, pendingDecisions: parsed.pendingDecisions ?? {} };
    this.prune();
  }

  save() {
    mkdirSync(dirname(this.path), { recursive: true, mode: 0o700 });
    const temporary = `${this.path}.${process.pid}.tmp`;
    writeFileSync(temporary, `${JSON.stringify(this.data)}\n`, { encoding: 'utf8', mode: 0o600 });
    chmodSync(temporary, 0o600);
    renameSync(temporary, this.path);
  }

  createInstallation({ bundleId, deviceToken, environment, preferences }) {
    const id = randomUUID();
    const deviceSecret = randomBytes(32).toString('base64url');
    const now = new Date().toISOString();
    this.data.installations[id] = {
      id,
      bundleId,
      deviceToken,
      environment,
      deviceSecretHash: hashSecret(deviceSecret),
      gateways: {},
      active: true,
      preferences: normalizePreferences(preferences),
      createdAt: now,
      updatedAt: now,
    };
    this.save();
    return { installation: publicInstallation(this.data.installations[id]), deviceSecret };
  }

  authenticate(id, secret, scope) {
    const installation = this.data.installations[id];
    if (!installation?.active || !secret) return null;
    const expected = scope === 'gateway' ? installation.gatewaySecretHash : installation.deviceSecretHash;
    return expected && hashSecret(secret) === expected ? installation : null;
  }

  authenticateGateway(installationId, gatewayId, secret) {
    const installation = this.data.installations[installationId];
    const gateway = installation?.gateways?.[gatewayId];
    if (!installation?.active || !gateway || !secret) return null;
    return hashSecret(secret) === gateway.secretHash ? { installation, gateway } : null;
  }

  updateInstallation(id, changes) {
    const installation = this.data.installations[id];
    if (!installation) return null;
    if (changes.deviceToken) installation.deviceToken = changes.deviceToken;
    if (changes.preferences) installation.preferences = normalizePreferences({ ...installation.preferences, ...changes.preferences });
    installation.active = changes.active ?? installation.active;
    installation.updatedAt = new Date().toISOString();
    this.save();
    return publicInstallation(installation);
  }

  deactivateInstallation(id) {
    return this.updateInstallation(id, { active: false });
  }

  createPairing(installationId) {
    this.prune();
    for (const [codeHash, pairing] of Object.entries(this.data.pairings)) {
      if (pairing.installationId === installationId) delete this.data.pairings[codeHash];
    }
    const code = readableCode();
    const expiresAt = new Date(Date.now() + 10 * 60_000).toISOString();
    this.data.pairings[hashSecret(normalizeCode(code))] = { installationId, expiresAt };
    this.save();
    return { code, expiresAt };
  }

  claimPairing(code, gatewayName) {
    this.prune();
    const key = hashSecret(normalizeCode(code));
    const pairing = this.data.pairings[key];
    if (!pairing) return null;
    const installation = this.data.installations[pairing.installationId];
    delete this.data.pairings[key];
    if (!installation?.active) { this.save(); return null; }
    const gatewayId = randomUUID();
    const gatewaySecret = randomBytes(32).toString('base64url');
    installation.gateways ??= {};
    installation.gateways[gatewayId] = {
      id: gatewayId,
      name: String(gatewayName || 'Hermes gateway').slice(0, 80),
      secretHash: hashSecret(gatewaySecret),
      createdAt: new Date().toISOString(),
    };
    installation.updatedAt = new Date().toISOString();
    this.save();
    return { gatewayId, installationId: installation.id, gatewaySecret };
  }

  removeGateway(installationId, gatewayId) {
    const installation = this.data.installations[installationId];
    if (!installation?.gateways?.[gatewayId]) return false;
    delete installation.gateways[gatewayId];
    installation.updatedAt = new Date().toISOString();
    this.save();
    return true;
  }

  acceptEvent(installationId, eventId) {
    this.prune();
    const key = `${installationId}:${eventId}`;
    if (this.data.eventIds[key]) return false;
    this.data.eventIds[key] = Date.now();
    this.save();
    return true;
  }

  // ── Pending decisions (clarify answer loop) ─────────────────────────
  // A clarify decision the plugin pushes carries a plugin-minted request id
  // because the gateway's own clarify id is unreachable to plugins. The
  // device answers by that id; the plugin polls for the answer while its
  // middleware blocks the tool call. Approval decisions answer via the
  // gateway directly and never enter this store.

  savePendingDecision({ id, installationId, gatewayId, question, choices, deliverable = true }) {
    this.prune();
    this.data.pendingDecisions[id] = {
      installationId,
      gatewayId,
      question: String(question ?? ''),
      choices: Array.isArray(choices) ? choices.map(String) : [],
      // False when device preferences mean no card was shown; the gateway's
      // poller stops early instead of polling a decision nobody can answer.
      deliverable: Boolean(deliverable),
      createdAt: Date.now(),
    };
    const entries = Object.entries(this.data.pendingDecisions);
    if (entries.length > 256) {
      for (const [key] of entries.sort((a, b) => a[1].createdAt - b[1].createdAt).slice(0, entries.length - 256)) {
        delete this.data.pendingDecisions[key];
      }
    }
    this.save();
  }

  respondPendingDecision(installationId, id, answer) {
    this.prune();
    const decision = this.data.pendingDecisions[id];
    if (!decision || decision.installationId !== installationId) return 'unknown';
    if (decision.answer !== undefined) return 'already_answered';
    decision.answer = String(answer ?? '').slice(0, 2000);
    decision.answeredAt = Date.now();
    this.save();
    return 'answered';
  }

  pendingDecisionStatus(installationId, gatewayId, id) {
    this.prune();
    const decision = this.data.pendingDecisions[id];
    if (!decision || decision.installationId !== installationId || decision.gatewayId !== gatewayId) {
      return { status: 'unknown' };
    }
    if (decision.answer === undefined) {
      // Legacy records predate the flag; treat missing as deliverable so the
      // poller's behavior is unchanged for decisions parked by older relays.
      return { status: 'pending', deliverable: decision.deliverable !== false };
    }
    return { status: 'answered', answer: decision.answer };
  }

  prune() {
    const now = Date.now();
    for (const [key, pairing] of Object.entries(this.data.pairings)) {
      if (Date.parse(pairing.expiresAt) <= now) delete this.data.pairings[key];
    }
    for (const [key, timestamp] of Object.entries(this.data.eventIds)) {
      if (Number(timestamp) < now - 24 * 60 * 60_000) delete this.data.eventIds[key];
    }
    // Clarify prompts live at most ~1h server-side (agent.clarify_timeout
    // default 3600s); 2h covers drift and unlimited-config edge cases while
    // still bounding the store.
    for (const [key, decision] of Object.entries(this.data.pendingDecisions ?? {})) {
      if (Number(decision.createdAt) < now - 2 * 60 * 60_000) delete this.data.pendingDecisions[key];
    }
  }
}

export function normalizePreferences(value = {}) {
  return Object.fromEntries(Object.entries(defaultPreferences).map(([key, fallback]) => [key, typeof value[key] === 'boolean' ? value[key] : fallback]));
}

function publicInstallation(installation) {
  return {
    id: installation.id,
    active: installation.active,
    gateways: Object.values(installation.gateways ?? {}).map((gateway) => ({ id: gateway.id, name: gateway.name })),
    preferences: installation.preferences,
    updated_at: installation.updatedAt,
  };
}

function hashSecret(value) {
  return createHash('sha256').update(value).digest('hex');
}

function normalizeCode(value) {
  return String(value).replace(/[^A-Za-z0-9]/g, '').toUpperCase();
}

function readableCode() {
  const alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';
  const bytes = randomBytes(10);
  const value = [...bytes].map((byte) => alphabet[byte % alphabet.length]).join('');
  return `${value.slice(0, 5)}-${value.slice(5)}`;
}
